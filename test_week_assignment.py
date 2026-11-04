"""Regression tests for LeagueLeader oldest-to-newest weekly matching."""
import unittest
from server import assign_candidate_weeks, GAME_ORDER

DATES={1:'10/14/2026',2:'10/21/2026',3:'10/28/2026'}
KIND=lambda x:'MPR' if x=='Cricket' else 'PPD'

def match(week,wins):
    return {'week':week,'teams':['Conway Banker','Opponent'],
            'formats':list(GAME_ORDER),'wins':wins,'sequence_valid':True}

def player_result(matches,played):
    ppd=[];mpr=[]
    for week in played:
        m=next(x for x in matches if x['week']==week)
        for position,kind in enumerate(m['formats']):
            data={'metric':KIND(kind),'rate':12.82 if week==3 and position==6 else
                  (2.1 if kind=='Cricket' else 17.0+week),
                  'win':m['wins'][position]==0,'game':kind,'achievements':{}}
            (mpr if kind=='Cricket' else ppd).append(data)
    return {'players':[{'name':'Joe Banker','team':'Conway Banker',
                        'games':ppd+mpr}]}

class WeeklyMappingTests(unittest.TestCase):
    def setUp(self):
        self.matches=[
            match(1,[0,1,0,1,0,1,0]),
            match(2,[0,0,1,1,0,1,1]),
            match(3,[1,0,1,0,1,0,0])
        ]
    def test_three_full_medleys_and_known_joe_week_three(self):
        result=assign_candidate_weeks(player_result(self.matches,[1,2,3]),self.matches)
        games=result['players'][0]['games']
        self.assertEqual(result['matching']['full_medley_assigned'],21)
        self.assertTrue(all(g.get('inferred_week') for g in games))
        self.assertEqual([sum(g.get('inferred_week')==i for g in games) for i in (1,2,3)],[7,7,7])
        self.assertEqual(next(g for g in games if g['rate']==12.82)['inferred_week'],3)
        self.assertEqual(next(g for g in games if g['rate']==12.82)['inferred_date'],DATES[3])

    def test_generic_x01_and_cricket_labels_use_team_format_order(self):
        report=player_result(self.matches,[1,2,3])
        for game in report['players'][0]['games']:
            game['game']='X01' if game['metric']=='PPD' else 'Cricket'
        result=assign_candidate_weeks(report,self.matches)
        self.assertEqual(result['matching']['full_medley_assigned'],21)
        joe=next(x for x in result['players'][0]['games'] if x['rate']==12.82)
        self.assertEqual(joe['inferred_week'],3)
        self.assertEqual(joe['inferred_game_number'],7)

    def test_missing_individual_game_format_label(self):
        report=player_result(self.matches,[1,3])
        for game in report['players'][0]['games']:
            game['game']=''
        result=assign_candidate_weeks(report,self.matches)
        self.assertEqual(result['matching']['full_medley_assigned'],14)
        self.assertEqual([r['week'] for r in result['players'][0]['weekly']],[1,3])

    def test_skipped_week_does_not_shift_calendar_assignment(self):
        result=assign_candidate_weeks(player_result(self.matches,[1,3]),self.matches)
        self.assertEqual(result['matching']['full_medley_assigned'],14)
        self.assertEqual([x['week'] for x in result['players'][0]['weekly']],[1,3])
        self.assertEqual(next(g for g in result['players'][0]['games'] if g['rate']==12.82)['inferred_week'],3)

    def test_organizer_anchor_fills_complete_week_three_game_block(self):
        report=player_result(self.matches,[3])
        # Simulate missing team-game matches from the live import.
        # The organizer identified the fourth X01 result (12.82)
        # as the final 01 game in Week 3.
        result=assign_candidate_weeks(report,[])
        games=result['players'][0]['games']
        self.assertEqual(len(games),7)
        self.assertTrue(all(g.get('inferred_week')==3 for g in games))
        self.assertEqual(sorted(g.get('inferred_game_number') for g in games),list(range(1,8)))
        self.assertEqual(result['matching']['assigned'],7)
        self.assertEqual(result['matching']['unmatched'],0)

    def test_no_unjustified_full_week_for_other_player(self):
        report=player_result(self.matches,[3])
        report['players'][0]['name']='Other Player'
        result=assign_candidate_weeks(report,[])
        self.assertEqual(result['matching']['assigned'],0)
        self.assertEqual(result['matching']['unmatched'],7)

    def test_ambiguous_week_not_guessed(self):
        identical=[match(i,[0,1,0,1,0,1,0]) for i in (1,2,3)]
        result=assign_candidate_weeks(player_result(identical,[2]),identical)
        self.assertEqual(result['matching']['full_medley_assigned'],0)
        self.assertEqual(result['matching']['ambiguous'],7)
        self.assertTrue(all('inferred_week' not in g for g in result['players'][0]['games']))

if __name__=='__main__':
    unittest.main()
