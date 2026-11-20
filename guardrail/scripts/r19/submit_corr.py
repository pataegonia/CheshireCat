# 제출 17회의 지연 지표와 가드·튜터 점수의 스피어먼 상관을 잰다. API 호출 0회.
import itertools, math
import random
def rank(v):
    o=sorted(range(len(v)),key=lambda i:v[i]);r=[0]*len(v);i=0
    while i<len(o):
        j=i
        while j+1<len(o) and v[o[j+1]]==v[o[i]]: j+=1
        for k in range(i,j+1): r[o[k]]=(i+j)/2+1
        i=j+1
    return r
def pear(a,b):
    n=len(a);ma=sum(a)/n;mb=sum(b)/n
    sa=sum((x-ma)**2 for x in a);sb=sum((y-mb)**2 for y in b)
    return sum((x-ma)*(y-mb) for x,y in zip(a,b))/math.sqrt(sa*sb) if sa and sb else float('nan')
def spearmanr(a,b):
    ra,rb=rank(a),rank(b);r=pear(ra,rb)
    random.seed(1);c=0;N=20000;rb2=rb[:]
    for _ in range(N):
        random.shuffle(rb2)
        if abs(pear(ra,rb2))>=abs(r)-1e-12:c+=1
    return r,c/N
# id, group, total, tutor, guard, t_p95, t_late, g_p95, g_12
R=[
("f8","A-main",83.81,77.33,90.30,11.72,0,10.90,5),
("f10","A-main",83.78,80.53,87.04,11.93,0,10.66,2),
("f12","A-main",84.72,78.39,91.04,11.78,0,11.92,14),
("f9","A-frz",88.69,84.89,92.50,15.12,3,10.93,6),
("f11","A-frz",87.07,86.19,87.95,17.64,7,11.39,9),
("f13","A-frz",88.02,86.11,89.93,16.86,5,11.93,15),
("n1","E-new",87.25,88.25,86.25,19.3,10,16.0,85),
("n2","E-new",86.30,84.58,88.01,17.2,5,14.8,59),
("n3","E-new",89.88,87.22,92.55,17.5,6,13.7,46),
("n4","E-new",88.41,84.42,92.41,16.6,1,13.9,44),
("n5","E-new",87.33,87.36,87.30,17.4,4,13.9,46),
("c1","E-cur",86.25,84.75,87.75,17.3,5,14.9,53),
("c2","E-cur",87.87,85.81,89.92,17.8,4,14.4,35),
("c3","E-cur",87.17,86.00,88.35,17.6,3,14.0,54),
("c4","E-cur",88.69,87.47,89.92,15.5,2,13.0,29),
("c5","E-cur",90.13,86.78,93.48,15.3,1,13.9,33),
("s26","D-main",84.92,81.86,87.99,20.01,32,19.28,53),
]
names=["id","grp","tot","tut","gd","tp95","tlate","gp95","g12"]
def col(rows,k): return [r[names.index(k)] for r in rows]
def resid(rows,k):
    out=[]
    g={}
    for r in rows: g.setdefault(r[1],[]).append(r)
    res={}
    for gr,rs in g.items():
        if len(rs)<2: continue
        m=sum(r[names.index(k)] for r in rs)/len(rs)
        for r in rs: res[r[0]]=r[names.index(k)]-m
    return res
def sp(rows,x,y,label,resid_mode=False):
    if resid_mode:
        rx,ry=resid(rows,x),resid(rows,y)
        ids=[r[0] for r in rows if r[0] in rx]
        a=[rx[i] for i in ids]; b=[ry[i] for i in ids]
    else:
        a=col(rows,x); b=col(rows,y)
    r,p=spearmanr(a,b)
    print(f"{label:55s} n={len(a):2d} rho={r:+.2f} p={p:.2f}")
sets={"all17":R,"no26(16)":[r for r in R if r[0]!="s26"],"E only(10)":[r for r in R if r[1].startswith("E")],"A only(6)":[r for r in R if r[1].startswith("A")]}
for sn,rows in sets.items():
    print("==",sn)
    for rm in (False,True):
        tag="resid" if rm else "raw"
        for x,y in [("gd","g12"),("gd","gp95"),("tut","tlate"),("tut","tp95"),("tot","g12"),("tot","tlate"),("gd","tlate"),("tut","g12"),("g12","tlate")]:
            sp(rows,y,x,f"{tag} {x} vs {y}",rm)
# 8000
G=[(90.60,12.3,16),(90.45,11.0,7),(91.65,11.3,8),(91.69,11.5,7),(91.77,12.22,22)]
print("8000 g12",spearmanr([g[0] for g in G],[g[2] for g in G]))
G2=G[:]+[(91.24,10.1,None),(90.81,12.2,None),(91.01,10.9,None)]
print("8000 p95",spearmanr([g[0] for g in G2],[g[1] for g in G2]))
