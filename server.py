"""Upper Deck league report importer with safe source diagnostics."""
from flask import Flask, jsonify, send_from_directory
from bs4 import BeautifulSoup
from datetime import datetime
import requests, re, json, pathlib, time

ROOT=pathlib.Path(__file__).parent
SCHEDULE=json.loads((ROOT/'schedule.json').read_text())
STANDINGS='https://leagueleader.net/sharedreport.php?operatorid=497&code=98aaaf95-13ba-40ae-800d-f6b6ffa9bb6e'
GAMES='https://leagueleader.net/sharedreport.php?operatorid=497&code=539607b8-de1c-4332-a95e-33c7a8584f89'
app=Flask(__name__)
cache={'at':0,'data':None}
HEADERS={'User-Agent':'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36','Accept':'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8'}

def norm(s):
    return re.sub(r'[^a-z]','',str(s).lower().replace('adolphson','adolfson').replace('bengier bengier','bengier'))

def fetch(url):
    response=requests.get(url,timeout=25,headers=HEADERS)
    response.raise_for_status()
    soup=BeautifulSoup(response.content,'html.parser')
    return response,soup

def rows(soup):
    output=[]
    for tr in soup.find_all('tr'):
        cells=tr.find_all(['td','th'],recursive=False)
        if not cells: cells=tr.find_all(['td','th'])
        row=[cell.get_text(' ',strip=True) for cell in cells]
        if row:output.append(row)
    return output

def _number(value):
    try:return float(str(value).replace(',', ''))
    except (TypeError, ValueError):return None

def get_standings(rr):
    """Detect report table headers instead of nonexistent section-title rows."""
    teams,players,cricket=[],[],[]
    mode=None
    for row in rr:
        if not row:continue
        header=[v.strip().lower() for v in row]
        if header[:4]==['team','win %','games','wins']:
            mode='teams';continue
        if header[:3]==['player','team','ppd']:
            mode='x01';continue
        if header[:3]==['player','team','mpr']:
            mode='cricket';continue
        if header[:3]==['team','against','date']:
            mode=None;continue
        if len(row)<4 or not row[0].strip() or row[0].startswith(('Division:', 'Summary:')):
            continue
        if mode=='teams':
            pct=_number(row[1]); games=_number(row[2]); wins=_number(row[3])
            if pct is None or games is None or wins is None:continue
            teams.append({'name':row[0], 'winPct':pct, 'games':int(games), 'wins':int(wins)})
        elif mode in ('x01','cricket'):
            avg=_number(row[2]); games=_number(row[3])
            if avg is None or games is None or not row[1]:continue
            if mode=='x01': players.append({'name':row[0], 'team':row[1], 'ppd':avg, 'games':int(games)})
            else: cricket.append({'name':row[0], 'team':row[1], 'mpr':avg, 'games':int(games)})
    # Use unique names to avoid accidental repeats from nested report tables.
    def unique(data):
        seen=set();result=[]
        for v in data:
            key=norm(v['name'])
            if key in seen:continue
            seen.add(key);result.append(v)
        return result
    return unique(teams),unique(players),unique(cricket)

def get_games(rr):
    """Location report rows are: team, week, timestamp, win%, games, wins, losses, type, against, at."""
    current=None;mode=False;out=[]
    allowed={'301 D/D','501 D/D','701 O/D','Cricket'}
    for row in rr:
        header=[str(v).strip().lower() for v in row]
        if header[:3]==['team','week','date']:
            mode=True;current=None;continue
        # Individual player summaries begin at the next table.
        if header[:2]==['team:',''] or (row and row[0].strip()=='Team:'):
            mode=False;current=None;continue
        if not mode:continue
        if len(row)>=10 and row[0].strip() and not row[2].strip():
            current=row[0].strip();continue
        if len(row)<10 or not current:continue
        date=row[2].strip();kind=row[7].strip();opponent=row[8].strip()
        if kind not in allowed or not opponent or not date:continue
        try:dt=datetime.strptime(date,'%m/%d/%Y %I:%M %p')
        except ValueError:continue
        wins=_number(row[5]); losses=_number(row[6])
        if wins is None or losses is None or wins+losses!=1:continue
        out.append({'team':current,'opponent':opponent,'datetime':date,'format':kind,'win':int(wins)})
    return out

def build_matches(raw):
    """Schedule determines the week. Use either team side, never duplicate mirrored records."""
    bykey={}
    for g in raw:
        try:dt=datetime.strptime(g['datetime'],'%m/%d/%Y %I:%M %p')
        except ValueError:continue
        key=(dt.date(),norm(g['team']),norm(g['opponent']))
        bykey.setdefault(key,[]).append((dt,g))
    output=[]
    for week in SCHEDULE:
        for m in week['matches']:
            home=m['home'];away=m['away']
            if home.startswith('Rank '):continue
            day=datetime.strptime(week['date'],'%m/%d/%Y').date()
            entries=bykey.get((day,norm(home),norm(away)),[])
            is_home=True
            if len(entries)!=7:
                entries=bykey.get((day,norm(away),norm(home)),[])
                is_home=False
            if len(entries)!=7:continue
            entries.sort(key=lambda item:item[0])
            wins=[(0 if g['win'] else 1) if is_home else (1 if g['win'] else 0) for _,g in entries]
            output.append({'week':week['num'],'teams':[home,away], 'wins':wins,
                           'times':[g['datetime'] for _,g in entries],
                           'formats':[g['format'] for _,g in entries]})
    return output

def report_diagnostic(url):
    try:
        response,soup=fetch(url)
        rr=rows(soup)
        body=' '.join(soup.stripped_strings)
        return {'status':response.status_code,'contentType':response.headers.get('content-type',''),'bytes':len(response.content),'title':soup.title.get_text(' ',strip=True)[:130] if soup.title else '', 'tableRows':len(rr),'firstRows':rr[:4], 'hasTeamStandings':'Team Standings' in body, 'hasDivision':'Division:' in body,'startsWith':body[:250]}
    except Exception as e:return {'error':str(e)[:300]}

def refresh():
    if cache['data'] and time.time()-cache['at']<300:return cache['data']
    _,soupS=fetch(STANDINGS);_,soupG=fetch(GAMES)
    tr=rows(soupS);gr=rows(soupG)
    teams,players,cricket=get_standings(tr);games=get_games(gr)
    if len(teams)!=18 or len(games)<100:raise ValueError(f'Unexpected LeagueLeader report layout: {len(teams)} teams, {len(games)} game records. No data was overwritten. Open /api/debug for diagnosis.')
    payload={'updated':datetime.now().astimezone().isoformat(timespec='seconds'),'teams':teams,'players':players,'cricket':cricket,'matches':build_matches(games),'records':len(games)}
    cache.update(at=time.time(),data=payload)
    return payload

@app.route('/')
def home():return send_from_directory(ROOT,'index.html')
@app.route('/api/live')
def live():
    try:return jsonify(refresh())
    except Exception as e:return jsonify({'error':str(e),'updated':cache['data']['updated'] if cache['data'] else None,'stale':cache['data']}),503
@app.route('/api/debug')
def debug():return jsonify({'standings':report_diagnostic(STANDINGS),'games':report_diagnostic(GAMES)})
if __name__=='__main__':app.run(host='0.0.0.0',port=8000,debug=False)
