#!/usr/bin/env python3
"""Back up Claude Code session JSONLs (`~/.claude/projects/*.jsonl`) to S3.

Incremental: uses `aws s3 sync --size-only`, so only new or grown files upload.
Same bucket can hold sessions from many people and/or headless agent boxes,
each at their own top-level prefix:

    s3://<bucket>/<actor>/local/<project>/<session>.jsonl     # a human's laptop
    s3://<bucket>/<remote-key>/<project>/<session>.jsonl      # a headless host

Local sessions go under `<actor>/local/`. Headless boxes are flat at the top
under their own `<remote-key>/` — they aren't "owned" by whoever ran the
backup. Actor is auto-derived from `aws sts get-caller-identity` (IAM user
name → actor via `actor_map`), or override with `--actor`.

# Configuration

All deployment-specific values (bucket name, IAM mappings, remote hosts) live
in a small JSON config so this script stays generic. Pass it via `--config`
or set `CLAUDE_BACKUP_CONFIG`. See `backup_config.example.json` for the shape.

The minimum config is one line — just the bucket name. Remote-host backup
is optional; without it, this only syncs the laptop's sessions.

# What happens on first run

If the bucket doesn't exist yet, it's created in the configured region with
versioning + block-public-access. If `remote_hosts` is non-empty, a bucket
policy is attached granting each remote's instance role write access to
its own `<remote-key>/*` prefix — so the SSM-triggered `aws s3 sync` on the
remote box can write directly without any IAM-role edits.

# Usage

    python3 backup_sessions.py --config ~/.config/claude-backup.json
    python3 backup_sessions.py --config ./cfg.json --local       # local only
    python3 backup_sessions.py --config ./cfg.json --remote      # remotes only
    python3 backup_sessions.py --config ./cfg.json --dry-run     # show plan
    python3 backup_sessions.py --config ./cfg.json --actor alice # override

Requires the `aws` CLI on PATH with credentials. For remote-host backup,
each remote must be an SSM-managed EC2 instance.

Apache 2.0 — see the repository LICENSE.
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path


DEFAULT_CONFIG_ENV = "CLAUDE_BACKUP_CONFIG"


def run(cmd, check=True, capture=True):
    return subprocess.run(cmd, check=check, text=True, capture_output=capture)


def load_config(path):
    if not path:
        path = os.environ.get(DEFAULT_CONFIG_ENV)
    if not path:
        sys.exit(
            "error: no config provided. Pass --config <path> or set "
            f"${DEFAULT_CONFIG_ENV}. See backup_config.example.json."
        )
    p = Path(path).expanduser()
    if not p.exists():
        sys.exit(f"error: config file not found: {p}")
    cfg = json.loads(p.read_text())
    if "bucket" not in cfg:
        sys.exit(f"error: config {p} missing required field 'bucket'.")
    cfg.setdefault("region", "us-east-1")
    cfg.setdefault("actor_map", {})
    cfg.setdefault("remote_hosts", [])
    return cfg


def derive_actor(cfg, override=None):
    if override:
        return override
    ident = json.loads(run(["aws", "sts", "get-caller-identity"]).stdout)
    arn = ident.get("Arn", "")
    m = re.search(r":user/([^:/]+)$", arn) or re.search(r":assumed-role/[^/]+/([^:/]+)$", arn)
    raw = m.group(1) if m else ident.get("UserId", "unknown")
    return cfg["actor_map"].get(raw, raw.lower())


def find_instance_id(name_tag):
    r = run([
        "aws", "ec2", "describe-instances",
        "--filters",
        f"Name=tag:Name,Values={name_tag}",
        "Name=instance-state-name,Values=running",
        "--query", "Reservations[].Instances[].InstanceId",
        "--output", "json",
    ])
    ids = json.loads(r.stdout)
    return ids[0] if ids else None


def bucket_policy_for_remotes(bucket, remote_hosts):
    statements = []
    for h in remote_hosts:
        role_arn = h.get("instance_role_arn")
        if not role_arn:
            continue
        statements.append({
            "Sid": f"Allow_{h['key']}_WriteOwnPrefix",
            "Effect": "Allow",
            "Principal": {"AWS": role_arn},
            "Action": [
                "s3:PutObject",
                "s3:GetObject",
                "s3:DeleteObject",
                "s3:AbortMultipartUpload",
            ],
            "Resource": f"arn:aws:s3:::{bucket}/{h['key']}/*",
        })
        statements.append({
            "Sid": f"Allow_{h['key']}_ListForSync",
            "Effect": "Allow",
            "Principal": {"AWS": role_arn},
            "Action": ["s3:ListBucket"],
            "Resource": f"arn:aws:s3:::{bucket}",
            "Condition": {
                "StringLike": {"s3:prefix": [f"{h['key']}/*", h['key']]}
            },
        })
    return {"Version": "2012-10-17", "Statement": statements} if statements else None


def ensure_bucket(cfg):
    bucket, region = cfg["bucket"], cfg["region"]
    head = run(["aws", "s3api", "head-bucket", "--bucket", bucket], check=False)
    if head.returncode == 0:
        return False
    print(f"[setup] creating bucket s3://{bucket} ({region})")
    create_cmd = ["aws", "s3api", "create-bucket", "--bucket", bucket, "--region", region]
    if region != "us-east-1":
        create_cmd += ["--create-bucket-configuration", f"LocationConstraint={region}"]
    run(create_cmd)
    run([
        "aws", "s3api", "put-bucket-versioning",
        "--bucket", bucket,
        "--versioning-configuration", "Status=Enabled",
    ])
    run([
        "aws", "s3api", "put-public-access-block",
        "--bucket", bucket,
        "--public-access-block-configuration",
        "BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true",
    ])
    policy = bucket_policy_for_remotes(bucket, cfg["remote_hosts"])
    if policy:
        print(f"[setup] attaching bucket policy for remotes: "
              f"{[h['key'] for h in cfg['remote_hosts']]}")
        run([
            "aws", "s3api", "put-bucket-policy",
            "--bucket", bucket,
            "--policy", json.dumps(policy),
        ])
    return True


def refresh_policy(cfg):
    """If remote_hosts changed since bucket creation, re-apply the policy."""
    bucket = cfg["bucket"]
    policy = bucket_policy_for_remotes(bucket, cfg["remote_hosts"])
    if not policy:
        return
    existing = run([
        "aws", "s3api", "get-bucket-policy",
        "--bucket", bucket,
        "--query", "Policy", "--output", "text",
    ], check=False).stdout.strip()
    new = json.dumps(policy, sort_keys=True)
    if existing and json.dumps(json.loads(existing), sort_keys=True) == new:
        return
    print("[setup] bucket policy out of date, re-applying for current remote_hosts")
    run([
        "aws", "s3api", "put-bucket-policy",
        "--bucket", bucket,
        "--policy", json.dumps(policy),
    ])


def sync_local(cfg, actor, dry_run=False):
    src = str(Path.home() / ".claude" / "projects") + "/"
    dst = f"s3://{cfg['bucket']}/{actor}/local/"
    if not Path(src.rstrip("/")).exists():
        print(f"[local] {src} not found, skipping")
        return
    print(f"\n[local] {src} -> {dst}")
    cmd = [
        "aws", "s3", "sync", src, dst,
        "--exclude", "*",
        "--include", "*.jsonl",
        "--size-only",
    ]
    if dry_run:
        cmd.append("--dryrun")
    run(cmd, capture=False)


def sync_remote(cfg, host, dry_run=False):
    iid = find_instance_id(host["name_tag"])
    if not iid:
        print(f"\n[{host['key']}] no running instance matching tag Name={host['name_tag']}, skipping")
        return
    remote_user = host.get("remote_user", "ubuntu")
    remote_root = host.get("remote_claude_dir") or f"/home/{remote_user}/.claude/projects/"
    dst = f"s3://{cfg['bucket']}/{host['key']}/"
    print(f"\n[{host['key']}] {iid}: {remote_root} -> {dst} (via SSM)")
    inner = (
        f"if [ -d {remote_root} ]; then "
        f"sudo -u {remote_user} aws s3 sync {remote_root} {dst} "
        f"--exclude '*' --include '*.jsonl' --size-only"
        + (" --dryrun" if dry_run else "")
        + f"; else echo NO_CLAUDE_DIR_ON_REMOTE; fi"
    )
    params = json.dumps({"commands": [inner]})
    cmd_id = run([
        "aws", "ssm", "send-command",
        "--instance-ids", iid,
        "--document-name", "AWS-RunShellScript",
        "--comment", f"claude_code_session_backup:{host['key']}",
        "--parameters", params,
        "--query", "Command.CommandId",
        "--output", "text",
    ]).stdout.strip()
    print(f"[{host['key']}] ssm command-id: {cmd_id}")
    last = ""
    while True:
        status = run([
            "aws", "ssm", "get-command-invocation",
            "--command-id", cmd_id,
            "--instance-id", iid,
            "--query", "Status", "--output", "text",
        ], check=False).stdout.strip()
        if status in ("Success", "Failed", "Cancelled", "TimedOut"):
            break
        if status != last:
            print(f"[{host['key']}] {status}…")
            last = status
        time.sleep(4)
    result = run([
        "aws", "ssm", "get-command-invocation",
        "--command-id", cmd_id,
        "--instance-id", iid,
        "--query", "{out:StandardOutputContent,err:StandardErrorContent}",
        "--output", "json",
    ]).stdout
    j = json.loads(result)
    if j.get("out"):
        print(j["out"])
    if status != "Success":
        print(f"[{host['key']}] FAILED ({status}). stderr:", file=sys.stderr)
        print(j.get("err") or "(no stderr)", file=sys.stderr)
        sys.exit(1)


def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--config", help=f"path to config JSON (or set ${DEFAULT_CONFIG_ENV})")
    ap.add_argument("--local", action="store_true", help="back up local (laptop) sessions")
    ap.add_argument("--remote", action="store_true", help="back up configured remote hosts via SSM")
    ap.add_argument("--actor", help="override actor folder name (default: derived from caller identity)")
    ap.add_argument("--dry-run", action="store_true", help="show what would upload, don't upload")
    ap.add_argument("--skip-setup", action="store_true", help="skip bucket create / policy apply")
    args = ap.parse_args()

    cfg = load_config(args.config)

    if not args.local and not args.remote:
        args.local = args.remote = True

    actor = derive_actor(cfg, args.actor)
    print(f"[actor] {actor}")
    print(f"[bucket] s3://{cfg['bucket']} ({cfg['region']})")

    if not args.skip_setup:
        created = ensure_bucket(cfg)
        if not created:
            refresh_policy(cfg)

    if args.local:
        sync_local(cfg, actor, dry_run=args.dry_run)
    if args.remote:
        for host in cfg["remote_hosts"]:
            sync_remote(cfg, host, dry_run=args.dry_run)

    print("\nDone.")


if __name__ == "__main__":
    main()
