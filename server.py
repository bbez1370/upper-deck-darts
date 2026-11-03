"""Upper Deck league: live LeagueLeader report importer. Run python server.py."""
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

def norm(s):"""Upper Deck league report importer with safe source diagnostics."""
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

def get_standings(rr):
    teams=[];players=[];cricket=[];mode=''
    for row in rr:
        s=' '.join(row)
        if 'Team Standings' in s:mode='teams';continue
        if 'Last Match Results' in s:mode='last';continue
        if 'All X01 games' in s:mode='x01';continue
        if 'All Cricket games' in s:mode='cricket';continue
        if mode=='teams' and len(row)>=4 and re.fullmatch(r'\d+(?:\.\d+)?',row[1] if len(row)>1 else '') and row[0]!='Team':
            teams.append({'name':row[0],'winPct':float(row[1]),'games':int(row[2]),'wins':int(row[3])})
        if mode=='x01' and len(row)>=4 and re.fullmatch(r'\d+(?:\.\d+)?',row[2] if len(row)>2 else ''):
            players.append({'name':row[0],'team':row[1],'ppd':float(row[2]),'games':int(row[3])})
        if mode=='cricket' and len(row)>=4 and re.fullmatch(r'\d+(?:\.\d+)?',row[2] if len(row)>2 else ''):
            cricket.append({'name':row[0],'team':row[1],'mpr':float(row[2]),'games':int(row[3])})
    return teams,players,cricket

def get_games(rr):
    mode=False;current=None;games=[]
    formats={'301 D/D','501 D/D','701 O/D','Cricket'}
    for row in rr:
        s=' '.join(row)
        if 'Team Standings' in s:mode=True;continue
        if 'All X01' in s or 'Team: ' in s:mode=False
        if not mode or not row:continue
        cell0=row[0].strip()
        if cell0 and not any(x in s for x in ('Totals:','Division:','Win %','Week','Games')) and not re.search(r'\d\d?/\d\d?/20\d\d',s):current=cell0;continue
        if not current:continue
        dm=re.search(r'\d{1,2}/\d{1,2}/20\d\d\s+\d{1,2}:\d\d\s*[AP]M',s)
        if not dm:continue
        kind=next((v for v in row if v.strip() in formats),None)
        if not kind:continue
        ix=row.index(kind)
        opponent=row[ix+1] if ix+1<len(row) else ''
        win=int(float(row[ix-2])) if ix>=2 and re.fullmatch(r'\d+(?:\.\d+)?',row[ix-2]) else None
        if win is None:continue
        games.append({'team':current,'opponent':opponent,'datetime':dm.group(0),'format':kind,'win':win})
    return games

def build_matches(raw):
    output=[]
    for w in SCHEDULE:
        for m in w['matches']:
            if m['home'].startswith('Rank '):continue
            desired=datetime.strptime(w['date'],'%m/%d/%Y').date()
            entries=[]
            for g in raw:
                if norm(g['team'])!=norm(m['home']) or norm(g['opponent'])!=norm(m['away']):continue
                try:
                    dt=datetime.strptime(g['datetime'],'%m/%d/%Y %I:%M %p')
                    if dt.date()==desired:entries.append((dt,g))
                except ValueError:continue
            entries.sort(key=lambda x:x[0])
            if len(entries)==7:
                output.append({'week':w['num'],'teams':[m['home'],m['away']],'wins':[0 if e['win'] else 1 for _,e in entries], 'times':[e['datetime'] for _,e in entries], 'formats':[e['format'] for _,e in entries]})
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
    if not teams or not games:raise ValueError(f'Unexpected LeagueLeader report layout: {len(teams)} teams, {len(games)} game records. No data was overwritten. Open /api/debug for diagnosis.')
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

    return re.sub('[^a-z]','',str(s).lower().replace('adolphson','adolfson').replace('bengier bengier','bengier'))

def rows(url):
    resp=requests.get(url,timeout=20,headers={'User-Agent':'Mozilla/5.0 (compatible; UpperDeckLeagueHub/1.0)'})
    resp.raise_for_status(); soup=BeautifulSoup(resp.text,'html.parser')
    return [[c.get_text(' ',strip=True) for c in tr.find_all(['td','th'],recursive=False)] for tr in soup.find_all('tr')]

def get_standings(rr):
    teams=[];players=[];cricket=[];mode=''
    for row in rr:
        s=' '.join(row)
        if 'Team Standings' in s:mode='teams';continue
        if 'Last Match Results' in s:mode='last';continue
        if 'All X01 games' in s:mode='x01';continue
        if 'All Cricket games' in s:mode='cricket';continue
        if mode=='teams' and len(row)>=4 and re.fullmatch(r'\d+(?:\.\d+)?',row[1] if len(row)>1 else '') and row[0]!='Team':
            teams.append({'name':row[0],'winPct':float(row[1]),'games':int(row[2]),'wins':int(row[3])})
        if mode=='x01' and len(row)>=4 and re.fullmatch(r'\d+(?:\.\d+)?',row[2] if len(row)>2 else ''):
            players.append({'name':row[0],'team':row[1],'ppd':float(row[2]),'games':int(row[3])})
        if mode=='cricket' and len(row)>=4 and re.fullmatch(r'\d+(?:\.\d+)?',row[2] if len(row)>2 else ''):
            cricket.append({'name':row[0],'team':row[1],'mpr':float(row[2]),'games':int(row[3])})
    return teams,players,cricket

def get_games(rr):
    # LeagueLeader's game report has the first table labelled "Team Standings, sorted by Name".
    # Team sections are header-only rows, followed by 7 dated rows per matchup.
    mode=False; current=None; games=[]
    for row in rr:
        s=' '.join(row)
        if 'Team Standings' in s: mode=True;continue
        if 'All X01' in s or 'Team: ' in s:mode=False
        if not mode or not row:continue
        cell0=row[0].strip()
        if cell0 and not any(x in s for x in ('Totals:','Division:','Win %','Week','Games')) and not re.search(r'\d\d?/\d\d?/20\d\d',s):
            current=cell0;continue
        if not current:continue
        date=next((re.search(r'\d{1,2}/\d{1,2}/20\d\d\s+\d{1,2}:\d\d\s*[AP]M',v) for v in row if re.search(r'\d{1,2}/\d{1,2}/20\d\d\s+\d{1,2}:\d\d\s*[AP]M',v)),None)
        if not date:continue
        at=date.group(0)
        formats=['301 D/D','501 D/D','701 O/D','Cricket']
        kind=next((v for v in row if v.strip() in formats),None)
        if not kind:continue
        ix=row.index(kind)
        opponent=row[ix+1] if ix+1<len(row) else ''
        # Report presents date, win %, Games, Wins, Losses, Type, Against.
        # Entries immediately preceding game type: [win %, 1, wins, losses].
        win=int(float(row[ix-2])) if ix>=2 and re.fullmatch(r'\d+(?:\.\d+)?',row[ix-2]) else None
        if win is None:continue
        games.append({'team':current,'opponent':opponent,'datetime':at,'format':kind,'win':win})
    return games

def build_matches(raw):
    # Deduplicate the symmetric entries by using scheduled home team's rows.
    output=[]
    for w in SCHEDULE:
        for m in w['matches']:
            if m['home'].startswith('Rank '):continue
            date=w['date'].lstrip('0').replace('/0','/')
            entries=[g for g in raw if norm(g['team'])==norm(m['home']) and norm(g['opponent'])==norm(m['away']) and g['datetime'].split(' ')[0].lstrip('0').replace('/0','/')==date]
            entries.sort(key=lambda g:datetime.strptime(g['datetime'],'%m/%d/%Y %I:%M %p'))
            if len(entries)==7:
                output.append({'week':w['num'],'teams':[m['home'],m['away']],'wins':[0 if e['win'] else 1 for e in entries], 'times':[e['datetime'] for e in entries], 'formats':[e['format'] for e in entries]})
    return output

def refresh():
    if cache['data'] and time.time()-cache['at']<300:return cache['data']
    tr=rows(STANDINGS);gr=rows(GAMES)
    teams,players,cricket=get_standings(tr)
    games=get_games(gr)
    if not teams or not games:raise ValueError(f'Unexpected LeagueLeader report layout: {len(teams)} teams, {len(games)} game records. No data was overwritten.')
    payload={'updated':datetime.now().astimezone().isoformat(timespec='seconds'),'teams':teams,'players':players,'cricket':cricket,'matches':build_matches(games),'records':len(games)}
    cache.update(at=time.time(),data=payload)
    return payload

@app.route('/')
def home():return send_from_directory(ROOT,'index.html')
@app.route('/api/live')
def live():
    try:return jsonify(refresh())
    except Exception as e:return jsonify({'error':str(e),'updated':cache['data']['updated'] if cache['data'] else None, 'stale':cache['data']}),503

if __name__=='__main__':app.run(host='127.0.0.1',port=8000,debug=False)
