"""Rank-only fusion, applied after scope, source and lifecycle validation.

RRF is also used by TencentDB-Agent-Memory (reviewed commit 2002196f).
This independent implementation does not compare incompatible raw score scales.
"""
def rrf(ranked_lists,allowed,k=60):
    scores={}
    for values in ranked_lists:
        seen=set();rank=0
        for ref in values:
            if ref in seen or ref not in allowed:continue
            seen.add(ref);rank+=1
            scores[ref]=scores.get(ref,0)+1/(k+rank)
    return scores
