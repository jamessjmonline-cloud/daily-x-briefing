import json, time, sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import xcli

SINCE=(datetime.now(timezone.utc)-timedelta(days=1)).date().isoformat()
CATS=[
 ('must_know','Must Know / Broad Viral','viral OR breaking OR "just in" OR announcement OR "watch this" OR unbelievable',1200),
 ('fast_risers','Fast Risers','viral OR breaking OR "just in" OR wild OR insane OR "going viral"',500),
 ('business_markets','Business & Markets','stocks OR markets OR earnings OR acquisition OR IPO OR layoffs OR "Wall Street" OR bitcoin OR crypto OR Fed OR inflation OR datacenter OR "data center"',250),
 ('it_security_msp','IT / Security / MSP','CVE OR ransomware OR breach OR exploited OR Microsoft OR CrowdStrike OR Fortinet OR "Palo Alto" OR Okta OR phishing OR "zero day" OR "security update" OR outage OR "Microsoft 365"',50),
 ('ai_tools','AI / Automation','AI OR OpenAI OR Anthropic OR Claude OR ChatGPT OR agent OR "AI tool" OR GitHub OR repo OR "voice cloning" OR "workflow automation"',250),
 ('culture_sports_ent','Culture / Sports / Entertainment','WorldCup OR "World Cup" OR NBA OR NFL OR MLB OR soccer OR football OR movie OR music OR celebrity OR Netflix OR "Love Island" OR Hollywood',800),
]
all_posts=[]; errors=[]

def add_posts(posts, cat):
    for p in posts:
        p['brief_cat']=cat
        all_posts.append(p)

for kind in ['for-you','following']:
    got, err = xcli.feed(kind, n=45)
    add_posts(got, 'feed_'+kind)
    if err: errors.append(err)
    print(f'feed {kind}: {len(got)}', file=sys.stderr, flush=True)
    time.sleep(2)

xcli._search_blocked=False
for key,title,q,min_likes in CATS:
    if xcli.search_blocked():
        errors.append(f'search skipped for {key}: rate-limited earlier this run')
        continue
    got, err = xcli.search(q, key, SINCE, min_likes, n=18)
    add_posts(got, key)
    if err: errors.append(err)
    print(f'search {key}: {len(got)}', file=sys.stderr, flush=True)
    time.sleep(5)

trends, terr = xcli.trending()
if terr: errors.append(terr)

def parse_dt(s):
    try:
        dt=datetime.fromisoformat(str(s).replace('Z','+00:00'))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None

def score(p):
    likes=p.get('likes',0) or 0; rt=p.get('retweets',0) or 0; rep=p.get('replies',0) or 0
    created=parse_dt(p.get('created_iso',''))
    age_h=12
    if created:
        age_h=max((datetime.now(timezone.utc)-created.astimezone(timezone.utc)).total_seconds()/3600, 0.75)
    velocity=likes/age_h
    p['velocity']=round(velocity,1)
    p['score']=velocity*2 + likes/250 + rt*0.4 + rep*0.2 + (100 if p.get('has_media') else 0)
    return p['score']

seen={}
for p in all_posts:
    if not p.get('id') or not p.get('text'): continue
    if (p.get('likes') or 0) < 10: continue
    key=p['id']
    if key not in seen:
        seen[key]=p
    else:
        seen[key]['sources']=sorted(set(seen[key].get('sources',[])) | set(p.get('sources',[])))
        if p.get('brief_cat') and seen[key].get('brief_cat','').startswith('feed_'):
            seen[key]['brief_cat']=p['brief_cat']
unique=list(seen.values())
for p in unique: score(p)
unique.sort(key=lambda p:p['score'], reverse=True)
cat_keywords={
 'business_markets':['stock','market','earnings','acquisition','ipo','layoff','wall street','bitcoin','crypto','fed','inflation','data center','datacenter','fund','investor','revenue','tariff'],
 'it_security_msp':['cve','ransomware','breach','exploit','exploited','microsoft','crowdstrike','fortinet','palo alto','okta','phishing','zero day','outage','patch','security','vulnerability','m365','office 365'],
 'ai_tools':['ai','openai','anthropic','claude','chatgpt','agent','github','repo','model','llm','voice cloning','automation','workflow','tool'],
 'culture_sports_ent':['world cup','worldcup','nba','nfl','mlb','soccer','football','ufc','movie','music','celebrity','netflix','hollywood','love island','fifa','england','mexico','norway','france','morocco'],
 'usa':['usa','u.s.','united states','america','american','trump','washington','new york','california','wall street','hollywood','nfl','nba','mlb','love island usa'],
 'worldwide':['world cup','fifa','england','mexico','norway','france','morocco','china','india','japan','europe','brazil','argentina','global','worldwide','canada'],
}
def matches(p, key):
    blob=(p.get('text','')+' '+p.get('author','')+' '+p.get('author_name','')).lower()
    return p.get('brief_cat')==key or any(k in blob for k in cat_keywords.get(key,[]))

def slim(p):
    return {k:p.get(k) for k in ['id','author','author_name','text','created_iso','likes','retweets','replies','views','url','has_media','brief_cat','velocity','score']}
sections={
 'must_know': unique[:8],
 'fast_risers': sorted(unique, key=lambda p:p.get('velocity',0), reverse=True)[:8],
 'business_markets': [p for p in unique if matches(p,'business_markets')][:8],
 'it_security_msp': [p for p in unique if matches(p,'it_security_msp')][:8],
 'ai_tools': [p for p in unique if matches(p,'ai_tools')][:8],
 'culture_sports_ent': [p for p in unique if matches(p,'culture_sports_ent')][:8],
 'usa': [p for p in unique if matches(p,'usa')][:8],
 'worldwide': [p for p in unique if matches(p,'worldwide')][:8],
}
payload={'collected_at':datetime.now().isoformat(timespec='seconds'), 'since':SINCE, 'post_count':len(unique), 'errors':errors[:8], 'sections':{k:[slim(p) for p in v] for k,v in sections.items()}, 'trends':trends[:15]}
Path('data/signal-now.json').write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding='utf-8')
print(json.dumps(payload, indent=2, ensure_ascii=False))
