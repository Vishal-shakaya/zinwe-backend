# Where the website lists come from

Built by `manage.py rebuild_blocklists` (`z_devices/blocklist_build.py`). Every source here was checked on 2026-11-01
for a licence that allows commercial use. Re-check when adding a source: terms change (see "Not used").

| List | Source | Licence (checked at) | How it's used |
|---|---|---|---|
| Adult, Gambling | Sinfonietta hostfiles, through the StevenBlack/hosts extensions | MIT (github.com/Sinfonietta/hostfiles LICENSE) | Candidate names |
| Adult | tiuxo hosts, through the StevenBlack/hosts extensions | CC BY 4.0 (github.com/tiuxo/hosts LICENSE): attribution required, given here | Candidate names |
| Adult, Gambling, Malware, Phishing & scams | ShadowWhisperer BlockLists (Adult, Gambling, Malware, Scam) | Unlicense, public domain (github.com/ShadowWhisperer/BlockLists LICENSE) | Candidate names |
| Malware | DigitalSide Threat-Intel, latest domains (github.com/davidonzo/Threat-Intel) | MIT (repo LICENSE; the author offers no support to companies) | Newest malware names, first in line |
| Phishing & scams | CERT Polska Warning List, hole.cert.pl (NASK-PIB) | "May be used by any user for any purpose, including commercial purposes"; attribution not required, and it must not imply any endorsement (cert.pl/en/posts/2020/03/malicious_domains) | Names added in the last 45 days, newest first |
| Phishing & scams | durablenapkin scamblocklist | MIT (repo LICENSE) | Fake shops and scam sites |
| All built lists | Majestic Million | CC BY 3.0 (majestic.com/reports/majestic-million): attribution required, given here | Only a filter: adult and gambling must be in it, malware and phishing must not |
| All built lists | Public Suffix List | MPL 2.0 (publicsuffix.org) | Only a filter: tells a site from a shared ending like github.io |
| Social media, Video streaming, `gambling-hand.txt`, `safelist.txt` | Written by hand for Zinwe | Ours | |

Dependence worth knowing: ShadowWhisperer says its lists are made from its own script and manual additions, and doesn't
name upstreams. The licence on the list itself is what we rely on.

## Not used, and why

| Source | Why not |
|---|---|
| URLhaus, ThreatFox, MalwareBazaar (abuse.ch) | No longer CC0 for us: abuse.ch's terms now say commercial or for-profit use of these "may require a paid subscription" through Spamhaus (abuse.ch/terms-of-use, section 4). Only Feodo Tracker, SSL Blacklist and YARAify stay CC0, and those are IP addresses and fingerprints, which a hosts file can't use. |
| Tranco top list | Its default list now mixes in Cloudflare Radar data under CC BY-NC 4.0 (tranco-list.eu). The Majestic Million does the same job under a commercial-friendly licence. |
| OpenPhish community feed | Its terms forbid any commercial purpose (openphish.com/terms.html). |
| Phishing.Database, Block List Project | MIT and Unlicense on the repo, but they merge upstream feeds and don't say which, and those feeds have included ones above. Avoided so a restricted feed doesn't come in through a side door. |
| Clefspeare13 pornhosts (a StevenBlack extension) | The upstream repository is gone (404), so its licence can't be checked. |

## How often to rebuild

- Malware and Phishing & scams: **every week**. Sites in these lists last days to weeks.
  `manage.py rebuild_blocklists`, then commit `blocklist_data/` and ship it with a release.
- Adult and Gambling: **every three months** is enough. `manage.py rebuild_blocklists --all`.
- Social media and Video streaming: by hand, when a service changes.

`manifest.json` holds the version, sizes and file hashes. PCs pick up a new version on their next heartbeat after the
deploy.
