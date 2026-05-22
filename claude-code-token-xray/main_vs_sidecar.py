"""Split the human-driven main thread from spawned subagents, and price both.

Main thread = depth-1 session logs; sidecar = nested `*/subagents/*.jsonl`.
Reports billed tokens, per-model mix, cache-hit rate, and Opus 4.7-rate cost for
each, plus the combined total and the sidecar's share. Reads only local logs.

Run: python3 main_vs_sidecar.py   (pip install tiktoken first)
"""
import json, glob, os, collections, tiktoken
enc = tiktoken.get_encoding("cl100k_base")
def ntok(s):
    if isinstance(s,list): s=" ".join(x.get("text","") for x in s if isinstance(x,dict))
    if not isinstance(s,str): s=json.dumps(s)
    return len(enc.encode(s, disallowed_special=()))

main_files = glob.glob(os.path.expanduser("~/.claude/projects/*/*.jsonl"))            # depth-1 = main thread
side_files = glob.glob(os.path.expanduser("~/.claude/projects/*/*/subagents/*.jsonl"))  # nested = sidecar agents

def analyze(files):
    R=dict(files=len(files), calls=0,
           billed=collections.Counter(), content=collections.Counter(),
           model_out=collections.Counter(), model_calls=collections.Counter(),
           per_call_prompt=[], per_call_out=[])
    for f in files:
        for line in open(f):
            line=line.strip()
            if not line: continue
            try: o=json.loads(line)
            except: continue
            m=o.get("message")
            if not isinstance(m,dict): continue
            role=m.get("role"); u=m.get("usage")
            if isinstance(u,dict) and role=="assistant":
                R["calls"]+=1
                ui=u.get("input_tokens",0)or 0; cr=u.get("cache_read_input_tokens",0)or 0
                cw=u.get("cache_creation_input_tokens",0)or 0; ot=u.get("output_tokens",0)or 0
                R["billed"]["uncached"]+=ui; R["billed"]["cache_read"]+=cr
                R["billed"]["cache_write"]+=cw; R["billed"]["output"]+=ot
                R["model_out"][m.get("model","?")]+=ot; R["model_calls"][m.get("model","?")]+=1
                R["per_call_prompt"].append(ui+cr+cw); R["per_call_out"].append(ot)
            c=m.get("content")
            if isinstance(c,str):
                if role=="user": R["content"]["user_prompt"]+=ntok(c)
                continue
            if not isinstance(c,list): continue
            for b in c:
                if not isinstance(b,dict): continue
                t=b.get("type")
                if t=="text":
                    R["content"]["assistant_text" if role=="assistant" else "user_prompt"]+=ntok(b.get("text",""))
                elif t=="tool_use": R["content"]["tool_calls"]+=ntok(b.get("input",{}))
                elif t=="tool_result":
                    R["content"]["tool_results"]+=ntok(b.get("content",""))
    return R

import statistics as st
def rate_cost(billed):
    return billed["cache_read"]/1e6*0.5 + billed["cache_write"]/1e6*10 + billed["uncached"]/1e6*5 + billed["output"]/1e6*25

def report(name,R):
    b=R["billed"]; inp=b["cache_read"]+b["cache_write"]+b["uncached"]; tot=inp+b["output"]
    print(f"\n############ {name} ############")
    print(f"files={R['files']:,}  assistant_calls={R['calls']:,}")
    print(f"  billed: read={b['cache_read']:,}  write={b['cache_write']:,}  uncached={b['uncached']:,}  output={b['output']:,}")
    print(f"  input:output = {inp/max(1,b['output']):.0f}:1   cache_hit={100*b['cache_read']/max(1,inp):.1f}%")
    if R['per_call_out']:
        print(f"  per-call: prompt mean={st.mean(R['per_call_prompt']):,.0f} median={st.median(R['per_call_prompt']):,.0f}  | output mean={st.mean(R['per_call_out']):,.0f} median={st.median(R['per_call_out']):,.0f}")
    # output decomposition (reasoning = output - visible generated)
    vis = R["content"]["assistant_text"]+R["content"]["tool_calls"]
    reason = b["output"]-vis
    print(f"  output decomp(est): reasoning~{reason:,} ({100*reason/max(1,b['output']):.0f}%)  tool_calls={R['content']['tool_calls']:,}  text={R['content']['assistant_text']:,}")
    print(f"  content(unique,tiktoken): tool_results={R['content']['tool_results']:,}  tool_calls={R['content']['tool_calls']:,}  asst_text={R['content']['assistant_text']:,}  user={R['content']['user_prompt']:,}")
    msplit = ", ".join(f"{(k.split('-')[-1] if k else k)}={v:,}" for k,v in R['model_out'].most_common())
    print("  model output split: " + msplit)
    print(f"  >> Opus-4.7-rate cost = ${rate_cost(b):,.0f}")

M=analyze(main_files); S=analyze(side_files)
report("MAIN THREAD", M)
report("SIDECAR (subagents)", S)
# combined
C=dict(billed=collections.Counter(), content=collections.Counter(), model_out=collections.Counter(),
       files=M["files"]+S["files"], calls=M["calls"]+S["calls"],
       per_call_prompt=M["per_call_prompt"]+S["per_call_prompt"], per_call_out=M["per_call_out"]+S["per_call_out"])
for k in M["billed"]: C["billed"][k]=M["billed"][k]+S["billed"][k]
for k in set(M["content"])|set(S["content"]): C["content"][k]=M["content"][k]+S["content"][k]
for k in set(M["model_out"])|set(S["model_out"]): C["model_out"][k]=M["model_out"][k]+S["model_out"][k]
report("COMBINED", C)
print(f"\nsidecar share of: calls={100*S['calls']/max(1,C['calls']):.1f}%  billed-tokens={100*(sum(S['billed'].values()))/max(1,sum(C['billed'].values())):.1f}%  cost={100*rate_cost(S['billed'])/max(1,rate_cost(C['billed'])):.1f}%")
