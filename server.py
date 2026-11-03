"""Upper Deck league report importer with safe source diagnostics."""
from flask import Flask, jsonify, send_from_directory
from bs4 import BeautifulSoup
from datetime import datetime
import requests, re, json, pathlib, time

ROOT=pathlib.Path(__file__).parent
SCHEDULE=json.loads((ROOT/'schedule.json').read_text())
STANDINGS='https://leagueleader.net/sharedreport.php?operatorid=497&code=98aaaf95-13ba-40ae-800d-f6b6ffa9bb6e'
GAMES='https://leagueleader.net/sharedreport.php?operatorid=497&code=539607b8-de1c-4332-a95e-33c7a8584f89'
GAME_ORDER=('301 D/D','Cricket','501 D/D','Cricket','701 O/D','Cricket','501 D/D')

def canonical_game_format(label):
    cleaned=re.sub(r'[^a-z0-9]','',str(label).lower())
    if cleaned.startswith('301'):return '301'
    if cleaned.startswith('501'):return '501'
    if cleaned.startswith('701'):return '701'
    if 'cricket' in cleaned:return 'cricket'
    return cleaned

def expected_sequence(formats):
    return len(formats)==7 and all(canonical_game_format(actual)==canonical_game_format(expected)
        for actual,expected in zip(formats,GAME_ORDER))

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
                           'formats':[g['format'] for _,g in entries],
                           'sequence_valid':expected_sequence([g['format'] for _,g in entries])})
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

@app.route('/register')
@app.route('/register.html')
def register():return send_from_directory(ROOT,'register.html')
@app.route('/api/live')
def live():
    try:return jsonify(refresh())
    except Exception as e:return jsonify({'error':str(e),'updated':cache['data']['updated'] if cache['data'] else None,'stale':cache['data']}),503
@app.route('/api/debug')
def debug():return jsonify({'standings':report_diagnostic(STANDINGS),'games':report_diagnostic(GAMES)})

# Additional public LeagueLeader report for player-level analytics.
PLAYER_DETAIL='https://leagueleader.net/sharedreport.php?operatorid=497&code=d512d685-0570-4906-ad06-8c03b903b500'

player_games_cache={'at':0,'payload':None}

def get_individual_games(rr):
    """Read per-player game rows from the Location Stats report.
    IMPORTANT: LeagueLeader prints week 0 on individual results, so these
    rows do not have verified calendar dates or schedule-week assignments.
    """
    section=None
    headers={}
    team=None
    player=None
    results={}
    scanned=0
    for row in rr:
        cells=[str(c).strip() for c in row]
        lower=[v.lower() for v in cells]
        if len(lower)>=6 and lower[2]=='week' and lower[3] in ('ppd','mpr') and 'games' in lower and 'wins' in lower:
            section=lower[3]
            headers={name:i for i,name in enumerate(lower) if name}
            team=None
            player=None
            continue
        if not section:
            continue
        if cells[0]=='Team:':
            team=cells[1] if len(cells)>1 else None
            player=None
            continue
        if len(cells)==2 and not cells[0] and cells[1] and team:
            player=cells[1]
            continue
        if not team or not player:
            continue
        def field(name):
            i=headers.get(name)
            return cells[i] if i is not None and i<len(cells) else ''
        if field('week')=='Summary:':
            continue
        n=_number(field(section))
        games=_number(field('games'))
        wins=_number(field('wins'))
        if n is None or games!=1 or wins not in (0,1):
            continue
        # A single genuine one-game row; never interpret a summary as a game.
        scanned+=1
        key=(norm(player),norm(team))
        item=results.setdefault(key,{'name':player,'team':team,'games':[]})
        achievements={}
        for label,i in headers.items():
            if label in ('week','ppd','mpr','games','wins','game',''):
                continue
            if i>=len(cells):continue
            v=_number(cells[i])
            if v is not None and v>0 and int(v)==v:
                achievements[label.upper()]=int(v)
        item['games'].append({
          'metric':section.upper(),'rate':round(n,2),'win':bool(wins),
          'game':field('game')[:40],
          'achievements':achievements
        })
    people=[]
    for item in results.values():
        item['summary']={}
        for typ in ('PPD','MPR'):
            group=[g for g in item['games'] if g['metric']==typ]
            if not group:continue
            values=[g['rate'] for g in group]
            count=len(values)
            avg=sum(values)/count
            spread=(sum((v-avg)**2 for v in values)/count)**0.5
            totals={}
            for g in group:
                for label,v in g['achievements'].items():
                    totals[label]=totals.get(label,0)+v
            item['summary'][typ]={
              'games':count,'average_game':round(avg,2),
              'best':max(values),'worst':min(values),'std_dev':round(spread,2),
              'wins':sum(1 for g in group if g['win']),
              'achievements':totals
            }
        people.append(item)
    people.sort(key=lambda p:(p['team'].lower(),p['name'].lower()))
    return {'source':'LeagueLeader Location Stats Report',
       'date_verified':False,'week_verified':False,
       'note':'Individual rows have Week 0 and no date. Game rows are displayed in report order only, not assigned to a league week.',
       'player_count':len(people),'game_count':scanned,'players':people}


def assign_candidate_weeks(result, matches):
    """Match ordered individual result sequences to dated team game slots.
    A week is assigned only if EVERY compatible order-preserving alignment
    puts the game in the same schedule week. Ambiguity stays unassigned.
    """
    schedule_dates={int(w['num']):w['date'] for w in SCHEDULE}
    matched=ambiguous=unmatched=0
    for person in result['players']:
        team=norm(person['team'])
        slots=[]
        for match in sorted(matches,key=lambda m:int(m['week'])):
            if team not in (norm(match['teams'][0]),norm(match['teams'][1])):
                continue
            for k,fmt in enumerate(match['formats']):
                if k>=len(match['wins']):continue
                if not match.get('sequence_valid',False):continue
                kind='MPR' if 'cricket' in str(fmt).lower() else 'PPD'
                slots.append({'metric':kind,'game':str(fmt),
                     'win':norm(match['teams'][match['wins'][k]])==team,
                     'week':int(match['week']),
                     'date':schedule_dates.get(int(match['week'])),
                     'game_number':k+1,
                     'is_deciding_game':k==6 and match['wins'][:6].count(0)==3 and match['wins'][:6].count(1)==3})
        for metric in ('PPD','MPR'):
            indexes=[i for i,g in enumerate(person['games']) if g['metric']==metric]
            seq=[person['games'][i] for i in indexes]
            options=[slot for slot in slots if slot['metric']==metric]
            m,n=len(seq),len(options)
            if not m:continue
            if not n or m>n:
                unmatched+=m
                continue
            def compatible(i,j):
                game=seq[i];slot=options[j]
                game_type=re.sub(r'[^a-z0-9]','',str(game.get('game','')).lower())
                slot_type=re.sub(r'[^a-z0-9]','',str(slot['game']).lower())
                return (game['win']==slot['win'] and game_type==slot_type)
            # Forward: first i player games can fit within first j team slots.
            forward=[[False]*(n+1) for _ in range(m+1)]
            forward[0][0]=True
            for j in range(n):
                for i in range(m+1):
                    if not forward[i][j]:continue
                    forward[i][j+1]=True
                    if i<m and compatible(i,j):forward[i+1][j+1]=True
            if not forward[m][n]:
                unmatched+=m
                continue
            # Suffix: games from i onward fit within slots from j onward.
            suffix=[[False]*(n+1) for _ in range(m+1)]
            for j in range(n+1):suffix[m][j]=True
            for j in range(n-1,-1,-1):
                for i in range(m-1,-1,-1):
                    suffix[i][j]=(suffix[i][j+1] or
                                  compatible(i,j) and suffix[i+1][j+1])
            for k,game in enumerate(seq):
                possible_slots=[options[j] for j in range(n)
                          if forward[k][j] and compatible(k,j) and suffix[k+1][j+1]]
                possible={slot['week'] for slot in possible_slots}
                if len(possible)==1:
                    week=next(iter(possible))
                    game_numbers={slot['game_number'] for slot in possible_slots}
                    if len(game_numbers)==1:
                        game['inferred_game_number']=next(iter(game_numbers))
                        game['inferred_decider']=all(slot['is_deciding_game'] for slot in possible_slots)
                    game['inferred_week']=week
                    game['inferred_date']=schedule_dates.get(week)
                    game['week_confidence']='unique_ordered_alignment'
                    matched+=1
                elif possible:
                    game['candidate_weeks']=sorted(possible)
                    ambiguous+=1
                else:
                    unmatched+=1
        byweek={}
        for g in person['games']:
            week=g.get('inferred_week')
            if week is None:continue
            key=str(week)
            item=byweek.setdefault(key,{'week':week,'date':schedule_dates.get(week),
                                        'PPD':[],'MPR':[]})
            item[g['metric']].append(g['rate'])
        person['weekly']=[{'week':v['week'],'date':v['date'],
            'PPD':{'games':len(v['PPD']),'average':round(sum(v['PPD'])/len(v['PPD']),2)} if v['PPD'] else None,
            'MPR':{'games':len(v['MPR']),'average':round(sum(v['MPR'])/len(v['MPR']),2)} if v['MPR'] else None}
            for v in sorted(byweek.values(),key=lambda x:x['week'])]
    result['matching']={'assigned':matched,'ambiguous':ambiguous,'unmatched':unmatched,
       'method':'Ordered per-player results aligned to seven-game medley sequence, matching team game type, win/loss and custom dated schedule',
       'game_order':list(GAME_ORDER),
       'validated_matches':sum(bool(m.get('sequence_valid')) for m in matches),
       'invalid_sequence_matches':sum(not bool(m.get('sequence_valid')) for m in matches),
       'caveat':'Weeks are inferred, not printed by LeagueLeader. A week is displayed only when every possible order-preserving match places the game there.'}
    result['week_verified']=False
    return result

@app.route('/api/player-games')
def individual_game_stats():
    """Standalone optional statistics API. A failure cannot break /api/live."""
    try:
        if player_games_cache['payload'] and time.time()-player_games_cache['at']<300:
            return jsonify(player_games_cache['payload'])
        _,soup=fetch(PLAYER_DETAIL)
        result=get_individual_games(rows(soup))
        official=refresh()
        result=assign_candidate_weeks(result,official['matches'])
        joe=next((p for p in result['players'] if norm(p['name'])=='joebanker'),None)
        if joe:
            last=next((g for g in reversed(joe['games']) if g['metric']=='PPD'),None)
            result['joe_last_01_check']={'last_ppd':last['rate'],'inferred_week':last.get('inferred_week'),
                'inferred_date':last.get('inferred_date'), 'candidate_weeks':last.get('candidate_weeks',[]),
                'matches_user_reported_12_82':abs(last['rate']-12.82)<0.005} if last else None
        if result['game_count']<20 or result['player_count']<5:
            raise ValueError('Unrecognized player game table layout; no fabricated records returned.')
        result['updated']=datetime.now().astimezone().isoformat(timespec='seconds')
        player_games_cache.update(at=time.time(),payload=result)
        return jsonify(result)
    except Exception as error:
        return jsonify({'error':str(error)[:200],
                        'stale':player_games_cache['payload']}),503

@app.route('/api/player-report-inspect')
def player_report_inspect():
    """Return bounded table samples to verify the public report schema before using player-level statistics."""
    try:
        response,soup=fetch(PLAYER_DETAIL)
        rr=rows(soup)
        samples=[]
        keywords=('player','date','ppd','mpr','highest','out','wins','week','cricket','301','501')
        for idx,row in enumerate(rr):
            joined=' '.join(str(x) for x in row).lower()
            if any(word in joined for word in keywords) and len(samples)<24:
                samples.append({'row':idx,'columns':row[:20],'following':rr[idx+1:idx+3]})
        return jsonify({'available':True,'status':response.status_code,'bytes':len(response.content),
                        'title':soup.title.get_text(' ',strip=True)[:200] if soup.title else '',
                        'row_count':len(rr),'first_rows':rr[:12],'section_samples':samples})
    except Exception as error:
        return jsonify({'available':False,'error':str(error)[:250]}),502

if __name__=='__main__':app.run(host='0.0.0.0',port=8000,debug=False)
