"""Per-activity *cumulative* input: what each kind of context costs once it's re-read every turn.

`token_time_breakdown.py` counts each token once (unique content). But Claude Code
re-sends the whole growing context on every call, so a token that enters early is
billed again and again. This script replays each session's context growth — for every
assistant call it adds the context present at that moment to a running tally — so you
get the **total re-read** per activity, not just the unique size.

Two numbers per activity:
  - unique      = distinct tokens of that kind (what you'd see once)
  - cumulative  = those tokens × the calls that re-read them (what you actually pay for)

The replay slightly overshoots the billed total (it assumes no compaction), so the
cumulative split is scaled to the measured billed input (cache reads + writes + fresh)
straight from the API `usage` blocks. The measured total is exact; the per-activity
split is a model.

Run: python3 reread_breakdown.py   (pip install tiktoken first)
Nothing leaves your machine; it only reads local files.
"""
import json, glob, os, collections, tiktoken
enc = tiktoken.get_encoding("cl100k_base")
def ntok(x):
    if isinstance(x, list): x=" ".join(b.get("text","") for b in x if isinstance(b,dict))
    return len(enc.encode(x if isinstance(x,str) else json.dumps(x), disallowed_special=()))

READ={"Read","Grep","Glob","WebSearch","WebFetch"}; SUB={"Agent","TaskOutput"}; EDIT={"Edit","Write","NotebookEdit"}
def tool_cat(nm):
    if nm in ("Bash","bash"): return "bash"
    if nm in SUB: return "subagents"
    if nm in EDIT: return "editing"
    return "reading"   # Read/Grep/Glob/web + anything unknown

uniq=collections.Counter(); cumu=collections.Counter(); measured=0
for f in glob.glob(os.path.expanduser("~/.claude/projects/*/*.jsonl")):
    ctx=collections.Counter(); pend={}; first=True   # ctx = context accumulated so far this session
    for line in open(f):
        try: o=json.loads(line)
        except: continue
        if o.get("type")=="attachment":
            s=ntok(o.get("attachment") or o.get("content") or {}); uniq["attachments"]+=s; ctx["attachments"]+=s; continue
        if o.get("isMeta") is True or o.get("type")=="system":
            mm=o.get("message") or {}
            s=ntok(mm.get("content","") if isinstance(mm,dict) else o.get("content",""))
            uniq["reminders"]+=s; ctx["reminders"]+=s
        m=o.get("message")
        if not isinstance(m,dict): continue
        role=m.get("role"); c=m.get("content"); u=m.get("usage") or {}
        if role=="assistant":
            if first:   # system prompt + tools land as the session's first cache write
                sp=u.get("cache_creation_input_tokens",0) or 0
                uniq["system"]+=sp; ctx["system"]+=sp; first=False
            # this call re-reads everything in context so far
            measured += (u.get("cache_read_input_tokens",0) or 0)+(u.get("cache_creation_input_tokens",0) or 0)+(u.get("input_tokens",0) or 0)
            for k,v in ctx.items(): cumu[k]+=v
            # then this turn's generated tokens become context for later calls
            ot=u.get("output_tokens",0) or 0; tc=0; sm=0
            if isinstance(c,list):
                for b in c:
                    if isinstance(b,dict):
                        if b.get("type")=="tool_use":
                            tc+=ntok(b.get("input",{})); pend[b.get("id")]=b.get("name")
                        elif b.get("type")=="text":
                            sm+=ntok(b.get("text",""))
            rn=max(0, ot-tc-sm)   # reasoning = billed output minus visible tool calls + text
            uniq["reasoning"]+=rn;   ctx["reasoning"]+=rn
            uniq["tool_calls"]+=tc;  ctx["tool_calls"]+=tc
            uniq["summaries"]+=sm;   ctx["summaries"]+=sm
        elif role=="user":
            if isinstance(c,str):
                s=ntok(c); uniq["instruction"]+=s; ctx["instruction"]+=s
            elif isinstance(c,list):
                for b in c:
                    if not isinstance(b,dict): continue
                    if b.get("type")=="text":
                        s=ntok(b.get("text","")); uniq["instruction"]+=s; ctx["instruction"]+=s
                    elif b.get("type")=="tool_result":
                        cat=tool_cat(pend.pop(b.get("tool_use_id"),None))
                        s=ntok(b.get("content","")); uniq[cat]+=s; ctx[cat]+=s

REPLAY=sum(cumu.values()); scale=measured/REPLAY if REPLAY else 1.0
def b(n): return f"{n/1e9:.2f}B" if n>=1e9 else (f"{n/1e6:.1f}M" if n>=1e6 else f"{n:,}")
print(f"unique total      = {sum(uniq.values()):,}")
print(f"billed input      = {measured:,}  (measured, exact)")
print(f"replay cumulative = {REPLAY:,}  (scaled by {scale:.2f} to match billed)\n")
print(f"{'activity':16s} {'unique':>10s} {'re-read':>10s}  share")
LABEL={"reasoning":"reasoning","reading":"reading/web","bash":"bash","system":"system+tools",
       "tool_calls":"tool calls","attachments":"attachments","summaries":"summaries",
       "instruction":"my prompt","reminders":"reminders","subagents":"subagents","editing":"editing"}
for k,_ in cumu.most_common():
    print(f"{LABEL.get(k,k):16s} {b(uniq[k]):>10s} {b(cumu[k]*scale):>10s}  {100*cumu[k]/REPLAY:4.1f}%")
