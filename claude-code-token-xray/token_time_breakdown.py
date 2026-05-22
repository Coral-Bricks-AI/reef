"""The headline table: where a month of Claude Code's tokens AND wall-clock go.

One pass over local logs (`~/.claude/projects/*/*.jsonl`), so tokens and time
stay consistent. Reasoning isn't stored in plaintext (only an encrypted
signature), so it's recovered by subtraction: `output - tool_calls - summaries`.
Time is reconstructed from event timestamps, not a profiler.

Run: python3 token_time_breakdown.py   (pip install tiktoken first)
Nothing leaves your machine; it only reads local files.
"""
import json, glob, os, collections, tiktoken
from datetime import datetime
enc = tiktoken.get_encoding("cl100k_base")
def ntok(x):
    if isinstance(x, list): x=" ".join(b.get("text","") for b in x if isinstance(b,dict))
    return len(enc.encode(x if isinstance(x,str) else json.dumps(x), disallowed_special=()))
def ts(o):
    t=o.get("timestamp")
    if not t: return None
    try: return datetime.fromisoformat(t.replace("Z","+00:00")).timestamp()
    except: return None
CAP=600.0
out_total=tok_call=tok_summary=tok_prompt=scaffold=attach=reminders=0
tr=collections.Counter(); toolt=collections.Counter(); gen=0.0
READ={"Read","Grep","Glob","WebSearch","WebFetch"}; SUB={"Agent","TaskOutput"}; EDIT={"Edit","Write","NotebookEdit"}
for f in glob.glob(os.path.expanduser("~/.claude/projects/*/*.jsonl")):
    evs=[]; pend={}; first=True
    for line in open(f):
        try:o=json.loads(line)
        except:continue
        if o.get("type")=="attachment":
            attach+=ntok(o.get("attachment") or o.get("content") or {}); continue
        if o.get("isMeta") is True or o.get("type")=="system":
            mm=o.get("message") or {}
            reminders+=ntok(mm.get("content","") if isinstance(mm,dict) else o.get("content",""))
        t=ts(o); m=o.get("message")
        if not isinstance(m,dict): continue
        role=m.get("role"); c=m.get("content"); u=m.get("usage") or {}
        kind=None
        if role=="assistant":
            kind="A"; out_total+=u.get("output_tokens",0) or 0
            if isinstance(u,dict) and first:
                scaffold+=u.get("cache_creation_input_tokens",0) or 0; first=False
            if isinstance(c,list):
                for b in c:
                    if isinstance(b,dict):
                        if b.get("type")=="tool_use":
                            tok_call+=ntok(b.get("input",{})); pend[b.get("id")]=(b.get("name"),t)
                        elif b.get("type")=="text":
                            tok_summary+=ntok(b.get("text",""))
        elif role=="user":
            human=isinstance(c,str) or (isinstance(c,list) and any(isinstance(b,dict) and b.get("type")=="text" for b in c))
            is_tr=isinstance(c,list) and any(isinstance(b,dict) and b.get("type")=="tool_result" for b in c)
            kind="H" if (human and not is_tr) else ("T" if is_tr else "H")
            if isinstance(c,str): tok_prompt+=ntok(c)
            elif isinstance(c,list):
                for b in c:
                    if not isinstance(b,dict): continue
                    if b.get("type")=="text": tok_prompt+=ntok(b.get("text",""))
                    elif b.get("type")=="tool_result":
                        tid=b.get("tool_use_id")
                        if tid in pend:
                            nm,t0=pend.pop(tid); tr[nm]+=ntok(b.get("content",""))
                            if t is not None and t0 is not None and 0<=t-t0: toolt[nm]+=min(t-t0,CAP)
                        else: tr["(unmatched)"]+=ntok(b.get("content",""))
        if t is not None and kind: evs.append((t,kind))
    evs.sort()
    for i in range(len(evs)-1):
        (t0,k0),(t1,k1)=evs[i],evs[i+1]; g=t1-t0
        if 0<=g and k1=="A" and k0 in ("T","H","A"): gen+=min(g,CAP)
reasoning=out_total-tok_call-tok_summary
known=READ|SUB|EDIT|{"Bash","bash","AskUserQuestion"}
bash_d=tr["Bash"]+tr["bash"]; sub_d=sum(tr[n] for n in SUB); edit_d=sum(tr[n] for n in EDIT)
read_d=sum(tr[n] for n in READ)+sum(v for n,v in tr.items() if n not in known)
bash_t=toolt["Bash"]+toolt["bash"]; sub_t=sum(toolt[n] for n in SUB); edit_t=sum(toolt[n] for n in EDIT)
read_t=sum(toolt[n] for n in READ)+sum(v for n,v in toolt.items() if n not in known)
sr=reasoning/out_total; sc=tok_call/out_total; ss=tok_summary/out_total
rows=[("Reasoning (hidden thinking)","output",reasoning,gen*sr),
 ("Running commands (Bash)","input",bash_d,bash_t),
 ("Writing tool calls","output",tok_call,gen*sc),
 ("Subagents & background jobs","input",sub_d,sub_t),
 ("Writing summaries","output",tok_summary,gen*ss),
 ("Reading / searching / web","input",read_d,read_t),
 ("Editing files","input",edit_d,edit_t),
 ("System prompt + tools + config","input",scaffold,None),
 ("Pasted attachments","input",attach,None),
 ("The instruction I typed","input",tok_prompt,None),
 ("Injected reminders","input",reminders,None)]
TT=sum(r[2] for r in rows); TM=sum(r[3] for r in rows if r[3] is not None)
def h(s): return f"{int(s//3600)}h {int((s%3600)//60):02d}m"
print(f"TOKEN TOTAL={TT:,}   TIME TOTAL={h(TM)}\n")
for n,io,tk,tm in rows:
    tms=f"{h(tm)} ({100*tm/TM:.0f}%)" if tm is not None else "—"
    print(f"{n:32s} {io:7s} {tk:11,} ({100*tk/TT:4.1f}%)  {tms:>13s}")
