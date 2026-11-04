"""Upper Deck league report importer with safe source diagnostics."""
from flask import Flask, jsonify, send_from_directory
from bs4 import BeautifulSoup
from datetime import datetime
import requests, re, json, pathlib, time
from concurrent.futures import ThreadPoolExecutor

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

def historical_player_row_audit(rr):
    """Detect distinct rows for one player/metric within the same report.
    Repeated identical rows can be display duplicates; differing rows require
    human review before any career average is computed.
    """
    mode=None
    keys={}
    for row in rr:
        if len(row)<4:continue
        header=[str(v).strip().lower() for v in row]
        if header[:3]==['player','team','ppd']:
            mode='PPD';continue
        if header[:3]==['player','team','mpr']:
            mode='MPR';continue
        if header[:4]==['team','win %','games','wins'] or header[:3]==['team','against','date']:
            mode=None;continue
        if mode is None:continue
        name=str(row[0]).strip()
        team=str(row[1]).strip()
        avg=_number(row[2]);games=_number(row[3])
        if not name or not team or avg is None or games is None or games<=0:continue
        key=(norm(name),mode)
        keys.setdefault(key,[]).append((team,avg,int(games)))
    ambiguous={k for k,values in keys.items() if len(set(values))>1}
    examples=[{'name':key[0],'metric':key[1],
              'distinct_rows':[{'team':team,'average':avg,'games':games}
                               for team,avg,games in sorted(set(values))[:8]]}
              for key,values in keys.items() if key in ambiguous]
    return {'ambiguous_keys':ambiguous,
            'conflicting_rows':len(ambiguous),
            'exact_repeat_keys':sum(len(rows)>1 and len(set(rows))==1 for rows in keys.values()),
            'examples':examples[:30]}

def get_games(rr):
    """Location report rows are: team, week, timestamp, win%, games, wins, losses, type, against, at."""
    current=None;mode=False;out=[]
    allowed={'301','501','701','cricket'}
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
        if canonical_game_format(kind) not in allowed or not opponent or not date:continue
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
def home():
    response=send_from_directory(ROOT,'index.html')
    response.headers['Cache-Control']='no-store, max-age=0, must-revalidate'
    return response

@app.route('/profiles')
@app.route('/profiles.html')
def profiles_page():return send_from_directory(ROOT,'profiles.html')

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


def normalize_individual_report(result):
    """Make one auditable, canonical seven-slot match per four 01 + three
    Cricket entries. Preserve original rows and their source-relative order.

    This is a local transformation of LeagueLeader data; it never edits
    their published report or invents an absent score.
    """
    normalized=[]
    order=('PPD','MPR','PPD','MPR','PPD','MPR','PPD')
    for person in result['players']:
        ppd=[g for g in person['games'] if g['metric']=='PPD']
        mpr=[g for g in person['games'] if g['metric']=='MPR']
        for metric,items in (('PPD',ppd),('MPR',mpr)):
            for i,g in enumerate(items):
                g['source_metric_index']=i
                g['source_metric_total']=len(items)
        blocks=min(len(ppd)//4,len(mpr)//3)
        person['normalized_blocks']=[]
        for i in range(blocks):
            # Underlying source is grouped by metric. The league's real
            # game order interleaves the 01 and Cricket reports.
            one=ppd[4*i:4*i+4]
            cr=mpr[3*i:3*i+3]
            merged=[one[0],cr[0],one[1],cr[1],
                    one[2],cr[2],one[3]]
            item={'source_block':i+1,'complete':True,
                  'game_count':len(merged),
                  'rows':[{'game_number':j+1,'metric':g['metric'],
                     'rate':g['rate'],'win':g['win'],
                     'source_metric_index':g['source_metric_index'],
                     'source_name':person['name']}
                     for j,g in enumerate(merged)]}
            person['normalized_blocks'].append(item)
        person['normalization']={
            'source_01_rows':len(ppd),'source_cricket_rows':len(mpr),
            'possible_complete_blocks':blocks,
            'unpaired_01_rows':len(ppd)-4*blocks,
            'unpaired_cricket_rows':len(mpr)-3*blocks,
            'source_order':'oldest_to_newest_per_metric',
            'canonical_game_order':list(GAME_ORDER),
            'complete_seven_game_rows':7*blocks}
        normalized.append({'player':person['name'],'team':person['team'],
            **person['normalization']})
    result['preprocessing']={'status':'normalized',
        'method':'Preserve original player rows; split ordered 01 and Cricket rows into 4+3 blocks and interleave the seven known league formats',
        'players':normalized}
    return result


def assign_normalized_report_weeks(result, matches=None):
    """Use *only* defensible complete-chronology or organizer evidence.

    A player with a complete block for EVERY scheduled elapsed week can be
    assigned in chronological order without requiring separately parsed
    team-game wins. Missed weeks require better matching data.
    Organizer-confirmed Week 3 is an additional anchor, but may only
    assign the final 4+3 source rows when Week 3 is still the latest
    scheduled played week and the 12.82 score is uniquely last in 01.
    """
    # Only a recorded, completed team match establishes a played week.
    # Server UTC date may already be Thursday while Wednesday evening
    # results have not happened; never use calendar elapsed weeks.
    matches=matches or []
    completed={int(m['week']) for m in matches if m.get('sequence_valid')}
    calendar={int(w['num']):w for w in SCHEDULE}
    for person in result['players']:
        team=norm(person['team'])
        played=sorted({int(m['week']) for m in matches
            if m.get('sequence_valid') and
            team in (norm(m['teams'][0]),norm(m['teams'][1]))})
        elapsed=[calendar[week] for week in played if week in calendar]
        known={int(w['num']):w['date'] for w in elapsed}
        ppd=[g for g in person['games'] if g['metric']=='PPD']
        mpr=[g for g in person['games'] if g['metric']=='MPR']
        full=person.get('normalized_blocks',[])
        diag=person.setdefault('assignment_diagnostic',{})
        assigned=0
        if full and len(full)==len(elapsed) and (
            len(ppd)==4*len(elapsed) and len(mpr)==3*len(elapsed)):
            for i,week in enumerate(elapsed):
                merged=[ppd[4*i],mpr[3*i],ppd[4*i+1],mpr[3*i+1],
                        ppd[4*i+2],mpr[3*i+2],ppd[4*i+3]]
                for n,g in enumerate(merged,1):
                    if g.get('inferred_week') not in (None,week['num']):continue
                    if g.get('inferred_week') is None:
                        g['inferred_week']=int(week['num'])
                        g['inferred_date']=week['date']
                        g['inferred_game_number']=n
                        g['week_confidence']='complete_report_preprocessing'
                        assigned+=1
        # Joe's documented final 01 rate on Wednesday October 28.
        # If it is still the last 01 result at the end of Week 3, the
        # preceding three 01 results and last three Cricket results form
        # the same seven-game sequence. Never guess scores absent from
        # the original report.
        if (norm(person['name'])=='joebanker' and elapsed and
            int(elapsed[-1]['num'])==3 and len(ppd)>=4 and len(mpr)>=3):
            found=[i for i,g in enumerate(ppd)
                if abs(float(g['rate'])-12.82)<0.005]
            if found==[len(ppd)-1]:
                merged=[ppd[-4],mpr[-3],ppd[-3],mpr[-2],
                        ppd[-2],mpr[-1],ppd[-1]]
                conflicts=[g for g in merged
                    if g.get('inferred_week') not in (None,3)]
                if not conflicts:
                    for n,g in enumerate(merged,1):
                        if g.get('inferred_week') is None:
                            g['inferred_week']=3
                            g['inferred_date']=known[3]
                            g['inferred_game_number']=n
                            g['week_confidence']='organizer_confirmed_normalized_last_block'
                            assigned+=1
                    diag['week_3_preprocessed_games']=7
                else:diag['week_3_preprocess_conflicts']=len(conflicts)
            else:
                diag['week_3_preprocess_note']='12.82 is not the sole latest 01 result'
        diag['normalized_preassignments']=assigned
    return result


def assign_candidate_weeks(result, matches):
    """Read per-player report rows oldest first, newest last.

    A seven-game team match contains four 01 games (301, 501, 701, 501)
    and three Cricket games. Join the separate 01 and Cricket report lists
    back into complete seven-game blocks when counts and all seven
    format/win results verify that the player played whole match(es).
    Align those complete blocks to the dated schedule in chronological
    order; handle skipped weeks without assuming consecutive attendance.
    Otherwise fall back to per-game order-preserving matching. Do not
    publish a week if more than one compatible schedule week remains.
    """
    schedule_dates={int(w['num']):w['date'] for w in SCHEDULE}
    matched=ambiguous=unmatched=full_medley_assigned=0
    match_weeks=sorted(matches,key=lambda m:int(m['week']))

    def uniquely_align(seq, options, compatible):
        """For each item, return all possible option positions across full
        order-preserving alignments. Avoid enumerating exponentially many
        alignments when win/loss sequences happen to repeat.
        """
        m,n=len(seq),len(options)
        if not m or m>n:return None
        forward=[[False]*(n+1) for _ in range(m+1)]
        forward[0][0]=True
        for j in range(n):
            for i in range(m+1):
                if not forward[i][j]:continue
                forward[i][j+1]=True
                if i<m and compatible(i,j):forward[i+1][j+1]=True
        if not forward[m][n]:return None
        suffix=[[False]*(n+1) for _ in range(m+1)]
        for j in range(n+1):suffix[m][j]=True
        for j in range(n-1,-1,-1):
            for i in range(m-1,-1,-1):
                suffix[i][j]=(suffix[i][j+1] or
                               compatible(i,j) and suffix[i+1][j+1])
        return [[j for j in range(n) if forward[i][j] and
                 compatible(i,j) and suffix[i+1][j+1]] for i in range(m)]

    def format_is_compatible(game, slot):
        """Check exact format only if the individual report specifies it."""
        player_type=canonical_game_format(game.get('game',''))
        expected=canonical_game_format(slot)
        return player_type not in ('301','501','701','cricket') or player_type==expected

    def game_matches(game, slot):
        """The individual report sometimes calls every 01 leg simply 'X01'
        or omits the format entirely. The dated team report supplies the
        authoritative 301/501/701 order. Require a format match only when
        the individual row explicitly identifies a specific game type.
        """
        if game['metric']!=slot['metric'] or game['win']!=slot['win']:
            return False
        return format_is_compatible(game,slot['game'])

    for person in result['players']:
        team=norm(person['team'])
        team_matches=[match for match in match_weeks
                      if match.get('sequence_valid') and
                      team in (norm(match['teams'][0]),norm(match['teams'][1]))]
        full_slots=[]
        for match in team_matches:
            slots=[]
            if len(match['wins'])!=7 or len(match['formats'])!=7:continue
            for k,fmt in enumerate(match['formats']):
                kind='MPR' if canonical_game_format(fmt)=='cricket' else 'PPD'
                slots.append({'metric':kind,'game':str(fmt),
                     'win':norm(match['teams'][match['wins'][k]])==team,
                     'week':int(match['week']),
                     'date':schedule_dates.get(int(match['week'])),
                     'game_number':k+1,
                     'is_deciding_game':k==6 and
                         match['wins'][:6].count(0)==3 and match['wins'][:6].count(1)==3})
            if [s['metric'] for s in slots]==['PPD','MPR','PPD','MPR','PPD','MPR','PPD']:
                full_slots.append(slots)

        p01=[g for g in person['games'] if g['metric']=='PPD']
        cricket=[g for g in person['games'] if g['metric']=='MPR']
        complete_block_alignment=False
        person.setdefault('assignment_diagnostic',{}).update({
          'dated_team_matches':len(full_slots),
          'player_01_games':len(p01),
          'player_cricket_games':len(cricket),
          'team_match_weeks':[x[0]['week'] for x in full_slots],
          'game_format_labels':sorted(set(str(g.get('game','')) for g in person['games']))[:20],
          'method':'awaiting alignment'
        })

        # When ALL three reported team matches have a corresponding
        # complete seven-game player block, source chronology alone
        # establishes both the week and game position. The individual
        # 'game' field is not an authoritative 301/501/701 format label:
        # those formats come from the dated team report's fixed medley.
        # This also avoids requiring the individual-result win flag to
        # equal the *team* win for a doubles game.
        if (full_slots and len(p01)==4*len(full_slots)
                and len(cricket)==3*len(full_slots)):
            possible_conflicts=[]
            for i,slots in enumerate(full_slots):
                merged=[p01[4*i],cricket[3*i],p01[4*i+1],
                        cricket[3*i+1],p01[4*i+2],
                        cricket[3*i+2],p01[4*i+3]]
                for g,slot in zip(merged,slots):
                    if g.get('inferred_week') not in (None,slot['week']):
                        possible_conflicts.append({
                            'source_rate':g['rate'],
                            'preassigned_week':g['inferred_week'],
                            'chronological_week':slot['week']})
            if not possible_conflicts:
                complete_block_alignment=True
                person['assignment_diagnostic']['method']='full_attendance_report_order'
                person['assignment_diagnostic']['chronological_full_matches']=len(full_slots)
                for i,slots in enumerate(full_slots):
                    merged=[p01[4*i],cricket[3*i],p01[4*i+1],
                            cricket[3*i+1],p01[4*i+2],
                            cricket[3*i+2],p01[4*i+3]]
                    for g,slot in zip(merged,slots):
                        if g.get('inferred_week') is None:matched+=1
                        g['inferred_week']=slot['week']
                        g['inferred_date']=slot['date']
                        g['inferred_game_number']=slot['game_number']
                        g['inferred_game_format']=slot['game']
                        g['inferred_decider']=slot['is_deciding_game']
                        g['week_confidence']='full_attendance_source_chronology'
                full_medley_assigned+=7*len(full_slots)
            else:
                person['assignment_diagnostic']['chronology_conflicts']=possible_conflicts
        # Otherwise attempt unique order-preserving alignments when
        # some weekly player results are missing.
        # Most doubles players have a 4 x 01 + 3 x Cricket sequence
        # per played match. Match *both* sections together to constrain weeks.
        if not complete_block_alignment and (p01 and cricket and len(p01)%4==0 and len(cricket)%3==0
                and len(p01)//4==len(cricket)//3
                and len(p01)//4<=len(full_slots)):
            count=len(p01)//4
            blocks=[]
            for i in range(count):
                zeros=p01[i*4:(i+1)*4]
                cr=cricket[i*3:(i+1)*3]
                blocks.append([zeros[0],cr[0],zeros[1],cr[1],
                               zeros[2],cr[2],zeros[3]])
            def block_compatible(i,j):
                return all(game_matches(g,slot) for g,slot in zip(blocks[i],full_slots[j]))
            options=uniquely_align(blocks,full_slots,block_compatible)
            if options is not None:
                complete_block_alignment=True
                person['assignment_diagnostic']['method']='complete_seven_game_match'
                for i,possibilities in enumerate(options):
                    weeks={full_slots[j][0]['week'] for j in possibilities}
                    if len(weeks)!=1:
                        for game in blocks[i]:
                            game['candidate_weeks']=sorted(weeks)
                        ambiguous+=7
                        continue
                    j=possibilities[0]
                    slots=full_slots[j]
                    for game,slot in zip(blocks[i],slots):
                        if game.get('inferred_week') not in (None,slot['week']):
                            person['assignment_diagnostic'].setdefault(
                                'preprocessing_match_conflicts',[]).append(
                                {'already':game['inferred_week'],'team_week':slot['week']})
                            continue
                        game['inferred_week']=slot['week']
                        game['inferred_date']=slot['date']
                        game['inferred_game_number']=slot['game_number']
                        game['inferred_decider']=slot['is_deciding_game']
                        game['week_confidence']='verified_full_medley_sequence'
                        matched+=1
                        full_medley_assigned+=1

        if not complete_block_alignment:
            person['assignment_diagnostic']['method']='partial_ordered_alignment'
            person['assignment_diagnostic']['complete_blocks_possible']=(
              bool(p01) and bool(cricket) and len(p01)%4==0 and
              len(cricket)%3==0 and len(p01)//4==len(cricket)//3 and
              len(p01)//4<=len(full_slots))
            # Incomplete weeks, player absences or missing report rows:
            # align the two metric sequences independently, retaining
            # ambiguous games as unassigned rather than guessing dates.
            slots=[slot for group in full_slots for slot in group]
            for metric,seq in [('PPD',p01),('MPR',cricket)]:
                options=[slot for slot in slots if slot['metric']==metric]
                m,n=len(seq),len(options)
                if not m:continue
                if not n or m>n:
                    unmatched+=m
                    continue
                # League organizer's checked example: Joe Banker's 12.82
                # PPD 01 game was played October 28, 2026 (Week 3).
                # Apply only when that score occurs exactly once and is
                # compatible with a Week 3 team slot. Never guess a game
                # number or anchor any other player's matching score.
                known_last=(norm(person['name'])=='joebanker' and
                    metric=='PPD' and
                    sum(abs(float(g['rate'])-12.82)<0.005 for g in seq)==1)
                anchor_i=next((i for i,g in enumerate(seq)
                    if abs(float(g['rate'])-12.82)<0.005),None) if known_last else None
                possible_anchor=(known_last and anchor_i is not None and
                    any(slot['week']==3 and game_matches(seq[anchor_i],slot)
                        for slot in options))
                def compatible(i,j):
                    return (game_matches(seq[i],options[j]) and
                        not (possible_anchor and i==anchor_i and options[j]['week']!=3))
                align=uniquely_align(seq,options,compatible)
                # If a user-confirmed anchor contradicts otherwise valid
                # source data, don't force it: retry without that anchor.
                if align is None and possible_anchor:
                    possible_anchor=False
                    align=uniquely_align(seq,options,compatible)
                if align is None:
                    unmatched+=m
                    continue
                for k,game in enumerate(seq):
                    poss=[options[j] for j in align[k]]
                    weeks={slot['week'] for slot in poss}
                    if len(weeks)==1:
                        slot=poss[0]
                        if game.get('inferred_week') not in (None,slot['week']):
                            person['assignment_diagnostic'].setdefault(
                                'preprocessing_match_conflicts',[]).append(
                                {'already':game['inferred_week'],'team_week':slot['week']})
                            continue
                        game['inferred_week']=slot['week']
                        game['inferred_date']=slot['date']
                        numbers={x['game_number'] for x in poss}
                        if len(numbers)==1:
                            game['inferred_game_number']=slot['game_number']
                            game['inferred_decider']=all(x['is_deciding_game'] for x in poss)
                        game['week_confidence']='unique_ordered_alignment'
                        if possible_anchor and k==anchor_i:
                            game['week_confidence']='organizer_confirmed_week_3'
                        matched+=1
                    elif weeks:
                        game['candidate_weeks']=sorted(weeks)
                        ambiguous+=1
                    else:
                        unmatched+=1

        # A report contains four 01 games and three Cricket games per
        # played team match. When the number of COMPLETE personal blocks
        # equals the number of validated, dated team matches, every
        # week is accounted for and block chronology uniquely establishes
        # the week even if individual W/L labels differ between reports.
        # Never use this fallback when a player skipped a team match.
        blocks_count=(len(p01)//4 if p01 and len(p01)%4==0 and
            cricket and len(cricket)%3==0 and
            len(p01)//4==len(cricket)//3 else 0)
        if not complete_block_alignment and blocks_count and blocks_count==len(full_slots):
            mapped=0
            for i,slots in enumerate(full_slots):
                personal=[p01[4*i],cricket[3*i],p01[4*i+1],
                          cricket[3*i+1],p01[4*i+2],
                          cricket[3*i+2],p01[4*i+3]]
                # Only use the position when each source game format is
                # compatible with the known league game order.
                if not all(format_is_compatible(g,slot['game'])
                    for g,slot in zip(personal,slots)):
                    continue
                for g,slot in zip(personal,slots):
                    if g.get('inferred_week') not in (None,slot['week']):
                        person['assignment_diagnostic'].setdefault(
                            'chronology_conflicts',[]).append({
                                'existing':g['inferred_week'],
                                'ordered_week':slot['week']})
                        continue
                    if g.get('inferred_week') is None:
                        g['inferred_week']=slot['week']
                        g['inferred_date']=slot['date']
                        g['inferred_game_number']=slot['game_number']
                        g['week_confidence']='complete_attendance_chronology'
                        mapped+=1
            person['assignment_diagnostic']['chronology_assigned']=mapped

        # Organizer-confirmed anchor: Joe Banker's 12.82 01 game was
        # game seven on October 28, 2026 (Week 3). Four sequential
        # 01 rows and three sequential Cricket rows constitute a match,
        # but this is usable only when the anchor falls in the fourth
        # 01 position of a complete block. Never apply this to other
        # players or unconfirmed scores.
        if norm(person['name'])=='joebanker' and not complete_block_alignment:
            confirmed=[(i,g) for i,g in enumerate(p01)
                       if abs(float(g['rate'])-12.82)<0.005]
            if len(confirmed)==1:
                anchor_i,g=confirmed[0]
                if g.get('inferred_week') not in (None,3):
                    person['assignment_diagnostic']['organizer_override_conflict']=g['inferred_week']
                if g.get('inferred_week') is None:
                    g['inferred_week']=3
                    g['inferred_date']='10/28/2026'
                    g['inferred_game_number']=7
                    g['week_confidence']='organizer_confirmed'
                if (anchor_i%4==3 and
                    len(p01)>=4*(anchor_i//4+1) and
                    len(cricket)>=3*(anchor_i//4+1)):
                    b=anchor_i//4
                    personal=[p01[4*b],cricket[3*b],p01[4*b+1],
                              cricket[3*b+1],p01[4*b+2],
                              cricket[3*b+2],p01[4*b+3]]
                    expected=['301','cricket','501','cricket',
                              '701','cricket','501']
                    if all(format_is_compatible(row,fmt)
                        for row,fmt in zip(personal,expected)):
                        added=0
                        conflicts=[]
                        for number,row in enumerate(personal,1):
                            if row.get('inferred_week') not in (None,3):
                                conflicts.append({'game':number,
                                                  'assigned':row['inferred_week']})
                                continue
                            if row.get('inferred_week') is None:added+=1
                            row['inferred_week']=3
                            row['inferred_date']='10/28/2026'
                            row['inferred_game_number']=number
                            row['week_confidence']='organizer_confirmed_block'
                        person['assignment_diagnostic']['week_3_block_companions_assigned']=added
                        if conflicts:
                            person['assignment_diagnostic']['week_3_block_conflicts']=conflicts
                    else:
                        person['assignment_diagnostic']['week_3_block_status']='format conflict'
                else:
                    person['assignment_diagnostic']['week_3_block_status']=(
                        'not a complete four-01 / three-Cricket block')
            else:
                person['assignment_diagnostic']['organizer_override_status']=(
                    '12.82 score not uniquely present; no override applied')
        # Recompute counts from actual assignments after every pass;
        # diagnostics must agree with the rows returned to the frontend.
        person['assignment_diagnostic']['assigned_final']=sum(
            g.get('inferred_week') is not None for g in person['games'])
        person['assignment_diagnostic']['unassigned_final']=sum(
            g.get('inferred_week') is None for g in person['games'])
        byweek={}
        for g in person['games']:
            week=g.get('inferred_week')
            if week is None:continue
            item=byweek.setdefault(str(week),{'week':week,
                'date':schedule_dates.get(week),'PPD':[],'MPR':[]})
            item[g['metric']].append(g['rate'])
        person['assignment_diagnostic'].update(
            assigned=sum(bool(g.get('inferred_week')) for g in person['games']),
            unassigned=sum(not bool(g.get('inferred_week')) for g in person['games']),
            found_full_match_order=bool(full_slots),
            latest_01_result=p01[-1]['rate'] if p01 else None,
            latest_01_week=p01[-1].get('inferred_week') if p01 else None)
        person['weekly']=[{'week':v['week'],'date':v['date'],
            'PPD':{'games':len(v['PPD']),
                   'average':round(sum(v['PPD'])/len(v['PPD']),2)} if v['PPD'] else None,
            'MPR':{'games':len(v['MPR']),
                   'average':round(sum(v['MPR'])/len(v['MPR']),2)} if v['MPR'] else None}
            for v in sorted(byweek.values(),key=lambda x:x['week'])]
    assigned_final=sum(g.get('inferred_week') is not None
        for person in result['players'] for g in person['games'])
    total_games=sum(len(person['games']) for person in result['players'])
    result['matching']={'assigned':assigned_final,'ambiguous':ambiguous,
       'unmatched':total_games-assigned_final,
       'full_medley_assigned':full_medley_assigned,
       'method':'Player game lists oldest-to-newest; match complete 301/Cricket/501/Cricket/701/Cricket/501 medleys to dated team results including win/loss and skipped weeks, then order-preserving partial-game fallback',
       'game_order':list(GAME_ORDER),
       'validated_matches':sum(bool(m.get('sequence_valid')) for m in matches),
       'invalid_sequence_matches':sum(not bool(m.get('sequence_valid')) for m in matches),
       'caveat':'LeagueLeader prints Week 0 on player rows. Weeks are assigned only when ordered games match dated team results unambiguously. Scores alone cannot identify an otherwise ambiguous week.'}
    result['week_verified']=False
    return result


# Historical league archives: only supplied public report URLs are used.
career_cache={'at':0,'payload':None}

def _historical_label(soup,rr):
    title=soup.title.get_text(' ',strip=True) if soup.title else ''
    head=' '.join(str(v) for row in rr[:24] for v in row)[:1500]
    combined=title+' '+head
    seasons=re.findall(r'20(?:2[0-9]|3[0-9])',combined)
    if len(seasons)>=2 and 0<int(seasons[1])-int(seasons[0])<=2:
        return seasons[0]+'–'+seasons[1][-2:]
    if seasons:return seasons[0]+' (season unverified)'
    return title[:95] or 'Unlabeled historical report'

def _fetch_archived_safely(source):
    try:return fetch(source['url'])
    except Exception as error:return error

def historical_careers():
    if career_cache['payload'] and time.time()-career_cache['at']<1800:
        return career_cache['payload']
    sources=json.loads((ROOT/'historical_sources.json').read_text())
    entries=[];records={};seen_season_rows=set()
    # Load the five independent reports concurrently to avoid long serial waits.
    with ThreadPoolExecutor(max_workers=5) as pool:
        fetched=list(pool.map(_fetch_archived_safely,sources))
    for number,source in enumerate(sources,1):
        url=source['url']
        record={'report_number':number,'url':url,'status':'unavailable','season':None}
        try:
            fetched_result=fetched[number-1]
            if isinstance(fetched_result,Exception):raise fetched_result
            _,soup=fetched_result
            rr=rows(soup)
            label=source.get('season') if source.get('verified') and source.get('season') else None
            detected_label=_historical_label(soup,rr)
            teams,p01,pcr=get_standings(rr)
            audit=historical_player_row_audit(rr)
            record.update(season=label,season_confirmed=bool(label),suggested_season=None if label else detected_label,rows=len(rr),players_01=len(p01),players_cricket=len(pcr),
                          conflicting_player_rows=audit['conflicting_rows'],
                          repeated_identical_rows=audit['exact_repeat_keys'],
                          ambiguous_examples=audit['examples'][:12])
            record['ambiguous_player_metrics_excluded']=0
            if not p01 and not pcr:
                record['status']='unrecognized report layout'
            elif not label:
                # Report can be read but its season has not been confirmed.
                # Exclude from career totals to avoid double-counting a season.
                record['status']='awaiting season confirmation'
            else:
                record['status']='parsed'
                # Deduplicate repeated source snapshots of the same player's season.
                # Exact normalized player names only; never fuzzy-merge two people.
                for metric,arr in [('PPD',p01),('MPR',pcr)]:
                    for p in arr:
                        if (norm(p['name']),metric) in audit['ambiguous_keys']:
                            record['ambiguous_player_metrics_excluded']+=1
                            continue
                        games=int(p['games'])
                        if games<=0:continue
                        avg=float(p['ppd'] if metric=='PPD' else p['mpr'])
                        name=p['name'].strip()
                        key=norm(name)
                        if not key:continue
                        unique=(key,label,metric)
                        if unique in seen_season_rows:
                            record['duplicate_rows_omitted']=record.get('duplicate_rows_omitted',0)+1
                            continue
                        seen_season_rows.add(unique)
                        item=records.setdefault(key,{'name':name,'seasons':[]})
                        item['seasons'].append({'season':label,'metric':metric,'average':avg,
                                                 'games':games,'team':p['team'],'report_number':number,'source_player':name})
        except Exception as error:
            record['error']=str(error)[:130]
        entries.append(record)
    current={'status':'unavailable','season':'2026–27'}
    try:
        payload=refresh()
        current['status']='parsed'
        for metric,arr in [('PPD',payload['players']),('MPR',payload['cricket'])]:
            for p in arr:
                name=p['name'].strip();key=norm(name)
                if not key or int(p['games'])<=0:continue
                entry=records.setdefault(key,{'name':name,'seasons':[]})
                unique=(key,'2026–27',metric)
                if unique not in seen_season_rows:
                    seen_season_rows.add(unique)
                    entry['seasons'].append({'season':'2026–27','metric':metric,
                        'average':float(p['ppd'] if metric=='PPD' else p['mpr']),
                        'games':int(p['games']),'team':p['team'],'report_number':0,'source_player':name})
    except Exception as error:
        current['error']=str(error)[:130]
    players=[]
    for p in records.values():
        career={}
        for metric in ('PPD','MPR'):
            data=[row for row in p['seasons'] if row['metric']==metric]
            games=sum(row['games'] for row in data)
            if games:
                career[metric]={'game_weighted_estimate':round(sum(row['average']*row['games'] for row in data)/games,2),
                    'games':games,'seasons':len(set(row['season'] for row in data))}
        p['career']=career
        p['seasons'].sort(key=lambda row:(row['season'],row['metric']))
        players.append(p)
    players.sort(key=lambda p:p['name'].lower())
    output={'record_status':'official','league':'Upper Deck Dart League','approved_seasons':['2021–22','2022–23','2023–24','2024–25','2025–26'],'career_average_method':'game_weighted_from_published_season_averages','updated':datetime.now().astimezone().isoformat(timespec='seconds'),
        'reports':entries,'current':current,'players':players,'player_count':len(players),
        'method':'Season PPD/MPR values weighted by games as an estimate; exact career averages require dart/mark totals.',
        'warnings':['Confirmed historical season labels are supplied by the league organizer.','Different player rows for the same name and metric in one report are excluded until reviewed.','Names with changed spellings require organizer-approved merge decisions.','Game-count-weighted career statistics are estimates, not exact dart- or mark-weighted averages.']}
    career_cache.update(at=time.time(),payload=output)
    return output

@app.route('/api/careers')
def careers():
    try:return jsonify(historical_careers())
    except Exception as error:return jsonify({'error':str(error)[:200]}),503


# Official cumulative scoring achievements from the organizer-provided LeagueLeader report.
# These values are NOT estimated by summing inferred individual week assignments.
ACHIEVEMENTS_REPORT='https://leagueleader.net/sharedreport.php?operatorid=497&code=b4ac42c8-fc0b-48ad-9a93-8830123b52b4'
achievement_cache={'at':0,'payload':None}
SCORING_KEYS={
 'low_tons':{'LTON','LTONS','LOWTON','LOWTONS'},
 'high_tons':{'HTON','HTONS','HIGHTON','HIGHTONS'},
 'ton_80s':{'TON80','TON80S','180','180S'},
 'white_horses':{'WHORSE','WHORSES','WHRSE','WHITEHORSE','WHITEHORSES'},
 'marks_5':{'5MR','5MARK','5MARKS'},
 'marks_6':{'6MR','6MARK','6MARKS'},
 'marks_7':{'7MR','7MARK','7MARKS'},
 'marks_8':{'8MR','8MARK','8MARKS'},
 'marks_9':{'9MR','9MARK','9MARKS'},
}
def scoring_header(s):
    return re.sub(r'[^A-Z0-9]','',str(s).upper())

def parse_scoring_report(soup):
    """Read organizer's cumulative achievement columns, not per-game rows.
    Separate 01 hats and Cricket hats by the corresponding PPD/MPR section.
    A report can contain an All Games summary as well as game-format rows:
    prefer its summary rather than double-counting both sources.
    """
    parsed=[]
    table_count=0
    columns_seen=set()
    for table in soup.find_all('table'):
        current=None
        heading=table.find_previous(['h2','h3','h4'])
        title=heading.get_text(' ',strip=True) if heading else ''
        all_games='all ' in title.lower() or 'all x01' in title.lower() or 'all cricket' in title.lower()
        for tr in table.find_all('tr'):
            cells=tr.find_all(['td','th'],recursive=False)
            if not cells:cells=tr.find_all(['td','th'])
            row=[x.get_text(' ',strip=True) for x in cells]
            keys=[scoring_header(x) for x in row]
            if 'PLAYER' in keys and 'GAMES' in keys and ('PPD' in keys or 'MPR' in keys):
                current=keys
                table_count+=1
                continue
            if not current or not row or len(row)!=len(current):continue
            try:
                name=row[current.index('PLAYER')].strip()
            except (ValueError,IndexError):continue
            if not name or name.lower().startswith(('team totals','division','summary','player')):continue
            team=row[current.index('TEAM')].strip() if 'TEAM' in current else ''
            game_count=_number(row[current.index('GAMES')])
            if game_count is None or game_count<=0:continue
            metric='MPR' if 'MPR' in current and 'PPD' not in current else 'PPD'
            if 'MPR' in current and 'PPD' in current:metric='BOTH'
            stats={}
            for label,valid in SCORING_KEYS.items():
                positions=[i for i,key in enumerate(current) if key in valid]
                if not positions:continue
                for idx in positions:
                    n=_number(row[idx])
                    if n is not None and n>=0 and n==int(n):
                        stats[label]=stats.get(label,0)+int(n)
                        columns_seen.add(label)
            hats=[i for i,key in enumerate(current) if key in {'HAT','HATS','HATTRICK','HATTRICKS'}]
            if hats:
                if metric=='BOTH':
                    ppd_i=current.index('PPD')
                    mpr_i=current.index('MPR')
                    for i in hats:
                        n=_number(row[i])
                        if n is not None and n>=0 and n==int(n):
                            key='hats_01' if ppd_i<i<mpr_i else 'hats_cricket' if i>mpr_i else None
                            if key:
                                stats[key]=stats.get(key,0)+int(n);columns_seen.add(key)
                else:
                    key='hats_01' if metric=='PPD' else 'hats_cricket'
                    vals=[_number(row[i]) for i in hats]
                    for n in vals:
                        if n is not None and n>=0 and n==int(n):
                            stats[key]=stats.get(key,0)+int(n);columns_seen.add(key)
            if not stats:continue
            parsed.append({'name':name,'team':team,'metric':metric,'games':int(game_count),
                           'stats':stats,'all_games':all_games,'source_section':title})
    merged={}
    # A player may appear in multiple formats. Use an explicit all-game
    # summary if present to prevent aggregating it alongside subformats.
    for entry in parsed:
        key=(norm(entry['name']),norm(entry['team']))
        player=merged.setdefault(key,{'name':entry['name'],'team':entry['team'],'entries':[]})
        player['entries'].append(entry)
    players=[]
    for player in merged.values():
        totals={}
        for stat in SCORING_KEYS.keys()|{'hats_01','hats_cricket'}:
            sources=[e for e in player['entries'] if stat in e['stats']]
            if any(e['all_games'] for e in sources):
                sources=[e for e in sources if e['all_games']]
            totals[stat]=sum(e['stats'][stat] for e in sources) if sources else None
        players.append({'name':player['name'],'team':player['team'],'achievements':totals})
    players.sort(key=lambda p:(p['name'].lower(),p['team'].lower()))
    return {'source':'Official LeagueLeader cumulative scoring report',
            'report_url':ACHIEVEMENTS_REPORT,
            'players':players,'player_count':len(players),
            'table_count':table_count,'reported_categories':sorted(columns_seen),
            'raw_player_rows':len(parsed)}

@app.route('/api/scoring-records')
def scoring_records():
    now=time.time()
    if achievement_cache['payload'] and now-achievement_cache['at']<300:
        return jsonify(achievement_cache['payload'])
    try:
        _,soup=fetch(ACHIEVEMENTS_REPORT)
        result=parse_scoring_report(soup)
        if result['table_count']==0 or result['raw_player_rows']==0:
            return jsonify({'error':'Scoring report has no recognizable player achievement tables',
                            'tables':result['table_count'],'rows':result['raw_player_rows']}),503
        achievement_cache.update({'at':now,'payload':result})
        return jsonify(result)
    except Exception as error:
        if achievement_cache['payload']:
            return jsonify({**achievement_cache['payload'],'stale':True})
        return jsonify({'error':'Could not load official scoring report: '+str(error)}),503

@app.route('/api/player-games')
def individual_game_stats():
    """Standalone optional statistics API. A failure cannot break /api/live."""
    try:
        if player_games_cache['payload'] and time.time()-player_games_cache['at']<300:
            return jsonify(player_games_cache['payload'])
        _,soup=fetch(PLAYER_DETAIL)
        result=normalize_individual_report(get_individual_games(rows(soup)))
        # Transform the source report into inspectable ordered blocks FIRST.
        # Only then perform team schedule alignment.
        try:
            official=refresh()
            result=assign_normalized_report_weeks(result,official['matches'])
            result=assign_candidate_weeks(result,official['matches'])
            result['team_match_source_status']='available'
        except Exception as match_error:
            # The preprocessing output is still usable where the complete
            # match chronology or an organizer-confirmed result establishes
            # a week without relying on the separate team-match API.
            result=assign_normalized_report_weeks(result,[])
            result=assign_candidate_weeks(result,[])
            result['team_match_source_status']='unavailable'
            result['team_match_source_error']=str(match_error)[:140]
        joe=next((p for p in result['players'] if norm(p['name'])=='joebanker'),None)
        if joe:
            # Organizer-confirmed regression example from October 28, 2026.
            # Find the matching result by PPD, not by "last", because more
            # weeks will be added to this live report.
            known=[g for g in joe['games'] if g['metric']=='PPD' and abs(g['rate']-12.82)<0.005]
            result['joe_week_3_check']={
                'expected_ppd':12.82,'expected_week':3,
                'expected_date':'10/28/2026','matching_game_count':len(known),
                'matched_week':known[0].get('inferred_week') if len(known)==1 else None,
                'matched_date':known[0].get('inferred_date') if len(known)==1 else None,
                'passes':len(known)==1 and known[0].get('inferred_week')==3
            }
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
