UPPER DECK DART LEAGUE | LIVE-IMPORT PROTOTYPE

This is a prototype, not a hosted public website.

Open index.html directly to see the October 28 saved standings, schedule and sample match detail.

To import live LeagueLeader reports:
1. Install Python 3.10+ from python.org.
2. Open a terminal in this folder.
3. Run: pip install -r requirements.txt
4. Run: python server.py
5. Visit http://127.0.0.1:8000 in a browser.

The server re-fetches both shared LeagueLeader URLs every five minutes while in use and matches game results with the existing 23-week schedule. To make this accessible to league members, deploy it to a server with HTTPS.

Limitations: Game records need to match the scheduled date and exact opponent pairing; make-up matches may need an admin adjustment. LeagueLeader can change its HTML format and cause the importer to fail safely. The player board is a local-only demo, not a shared multi-user community.
