# x-ui MaxVPN Edition

An add-on edition for an [x-ui / 3x-ui](https://github.com/MHSanaei/3x-ui) hub that
relays users to proxies abroad - typically a server inside Iran forwarding traffic
through tunnels. It adds what the stock panel lacks, **without modifying the panel**
(so panel updates keep working): automatic failover between paths, a traffic split
that makes pages open faster, a DNS cache, one subscription link with several
configs, and an **Optimizer** page inside the panel's own menu to control it all.

The numbers below were measured on a production hub with about 140 users and two servers abroad.

| Tool | Runs on | What it does |
|---|---|---|
| `xui-optimizer` | hub | ad block + DNS cache + routeOnly sniffing |
| `split/xui-split` | hub | light traffic over mux, heavy traffic without; prepares the guard |
| `guard/tunnel-guard` | hub | probes each path every minute, fails users over without a restart, Telegram alerts, daily report |
| `guard/config-guard` | hub | puts the routing back the moment the panel drops it, and says so on Telegram |
| `guard/xui-path` | hub | adds or removes a backup path from a share link: outbound + probe port + guard entry |
| `guard/xui-tunnel` | hub | finds the tunnels, keeps several tunnel addresses per path, moves a path off a dead tunnel without a restart |
| `abroad/abroad-tune` | server abroad | prefer IPv4 on the way out (fixes sites that stall over IPv6) |
| `sysctl/99-proxy-tcp.conf` | both | TCP settings for long-lived proxied connections |
| `tools/mux-bench` | hub | measures whether mux pays off on your link |
| `tools/xui-fix-inbound-lists` | hub (3.x) | repairs "inbound N: empty client ID" when a client is linked to an inbound the inbound does not list |
| `tools/xui-probe-client` | hub (3.x) | connects with one user's own config and measures it, to answer "is it me or them?" |
| `tools/xui-subpage` | hub (3.x) | the panel's own subscription page in Persian, with an "expired / out of data" stamp; the panel's language is untouched |
| `migrate/` | hub | 2.x -> 3.x upgrade, and folding old duplicate-client tricks into real multi-inbound clients |
| `dash/` | hub | "Optimizer" page in the panel's sidebar: live traffic, users, a full check-up, a panel linter and every switch above |

All of them edit the panel's own database (so the panel keeps the changes and shows
them under **Xray Configs**), test the new config with `xray run -test` **before**
touching anything, back up the panel's database (`x-ui.db`, or `x-ui.pgdump` on PostgreSQL)
+ `config.json` to `/root/xui-backup-<time>/`, and replace their own rules on a re-run instead
of adding more.

### SQLite or PostgreSQL

3x-ui 3.x can keep its data in PostgreSQL instead of `/etc/x-ui/x-ui.db` (`XUI_DB_TYPE=postgres`,
`XUI_DB_DSN` in `/etc/default/x-ui`). Every tool goes through one module, `lib/xuidb.py`
(installed in `/usr/local/lib/xui-optimizer`, linked as `xui-db`), which finds the database the way
x-ui does: the running x-ui process's environment, then the service's settings, then the SQLite
default. PostgreSQL is reached with `psql` (the installer adds `postgresql-client`), credentials go
through libpq's environment, not the command line, and a tool's writes are sent together in one
transaction on commit, as with SQLite. Backups are a full `pg_dump`; `xui-db restore FILE` (or the
Optimizer page) puts one back, stopping and starting x-ui around it. `xui-db detect` shows what was
found. Only the 2.x -> 3.x upgrade stays SQLite-only: 2.x panels have no PostgreSQL.

## Install

**With git clone:**

```bash
git clone https://github.com/MaxTeller95/xui-optimizer.git
cd xui-optimizer && sudo bash install.sh            # add --dash 59085 for the Optimizer page
```

**With one command** (while the repository is private, with a GitHub token that can
read it; the token is used once and not stored on the server):

```bash
export GITHUB_TOKEN=<token>
bash <(curl -fsSL -H "Authorization: token $GITHUB_TOKEN" https://raw.githubusercontent.com/MaxTeller95/xui-optimizer/main/install.sh)
```

Once the repository is public: `bash <(curl -fsSL https://raw.githubusercontent.com/MaxTeller95/xui-optimizer/main/install.sh)`

The source is kept in `/opt/xui-optimizer`; the tools go to `/usr/local/sbin`.
**To update**, run the installer again - it pulls the latest version and refreshes the
Optimizer page if it is installed, without touching your panel settings.

Nothing is applied by installing. Suggested order on a new hub:

```bash
xui-optimizer --dry-run && xui-optimizer
xui-split && xui-split apply
cp sysctl/99-proxy-tcp.conf /etc/sysctl.d/ && sysctl -p /etc/sysctl.d/99-proxy-tcp.conf
nano /etc/tunnel-guard.json && tunnel-guard --check && tunnel-guard --test
systemctl enable --now tunnel-guard.timer
config-guard && systemctl enable --now config-guard.path config-guard.timer
```

On each server abroad: `abroad-tune && abroad-tune apply`, plus the sysctl file.

---

## xui-optimizer - ad block and DNS cache

- **Ad block** - `geosite:category-ads-all` goes to the blackhole outbound for every
  inbound routed to a proxy. Ads and trackers never cross the link.
- **DNS cache** - users' DNS (port 53) is answered by Xray on the hub. Only misses
  leave, as DoH to 1.1.1.1 / 8.8.8.8 through the proxy - never to local resolvers,
  which in Iran are tampered with. **~100 ms for a miss, 0.1 ms for a hit.**
- **Sniffing** with `routeOnly`, so the ad block also works when a client sends bare
  IPs. The connection target is not rewritten.

```bash
xui-optimizer --dry-run   # build and test, change nothing
xui-optimizer             # back up, test, apply, restart x-ui
xui-optimizer --remove    # undo (sniffing is left as it is)
xui-optimizer --adblock on|off     # only the ad block; no other rule moves
xui-optimizer --quic block|allow   # only the QUIC (UDP 443) block
```

**QUIC block.** YouTube, Instagram and browsers try QUIC (HTTP/3 over UDP 443) first. Inside a
TCP tunnel it rides under two congestion controls and is the first thing throttled on the way;
refused, the apps reconnect over HTTPS/TCP within a second, which inside a tunnel is usually
faster and steadier. On a production hub about 14 % of user connections were QUIC; with the
block on every QUIC attempt was refused and the same apps came back over TCP. The Optimizer
page shows the QUIC share and how many were turned back, from the access log.

`--dns-via OUTBOUND` picks the path for DoH, `--no-adblock` / `--no-dns` skip a part.
Inbounds routed to `direct` are left alone. Devices using Private DNS / DoH bypass the
cache; the ad block still applies.

**Own rules that must win** (for example "these users reach Iranian sites directly,
including their Iranian DNS") belong *above* the DNS cache rule, and must not also be
limited to the `api` inbound - rules match only when every condition holds.

## xui-split - mux only where it helps

A new user connection costs a TCP handshake across the tunnel (~84 ms on an
Iran-Germany path). Mux removes it, but puts several streams on one TCP connection:

| (mux-bench, same outbound) | page, 20 connections | 1 download | 8 downloads |
|---|---|---|---|
| plain | 1727 ms | 438 Mbit/s | **815 Mbit/s** |
| mux, concurrency 4 | **1399 ms** | 374 Mbit/s | 477 Mbit/s |
| mux, concurrency 8 | 1405 ms | 387 Mbit/s | 441 Mbit/s |

Pages open ~19% faster, but aggregate download is capped near 480 Mbit/s and a big
download would share a connection with other people's chats. So `xui-split` sends:

- **light** (everything else: pages, messengers, APIs) -> `<MAIN>-mux`
- **heavy** (googlevideo, Instagram/Facebook CDN, Netflix, Twitch, Spotify, OS and app
  updates, GitHub/Docker downloads, speed tests), **UDP** and **Telegram** -> `<MAIN>`

The hub's busiest hour averaged 26 Mbit/s, far below the mux cap. Run
`mux-bench <OUTBOUND>` to check your own link before applying.

**One split per exit.** A hub can send users to several servers in several countries. Every
outbound a catch-all rule sends users to is a group of its own, with its own `<OUT>-mux`, its
own rules (`split-<out>-udp/heavy/telegram` -> `<OUT>`, `split-<out>-light` -> `<OUT>-mux`,
before `main-<out>`), its own probe port `guard-<out>-mux` and its own settings in
`/etc/xui-split.json` (on/off, concurrency, xudp, a label such as the country, extra heavy
domains). Other routes to the same server are not groups. tunnel-guard watches every mux on
its own and moves only that exit's light traffic when its mux stalls.

```bash
xui-split                 # test (default)
xui-split apply           # also turns on the panel's daily Telegram backup
xui-split remove
xui-split groups                              # the exits, their inbounds and settings
xui-split set BACKUP --label "site 2" --concurrency 8   # one exit; then fix (mux only) or apply (on/off)
```

`xui-split check` tells whether the split really works: `<MAIN>-mux` exists, has mux on,
matches the main outbound (address, sing-box route, credentials), the split rules are in
order, the probe port is there, and the running Xray has mux too. `xui-split fix` repairs
what it found - through Xray's API with **no restart** when only the mux outbound is wrong,
otherwise with a rebuild and a restart. The panel's own outbound editor can save the mux
copy without its `mux` block; config-guard notices after every write of `config.json` and
puts it back live. The page shows the real mux state under the split switch, with a
repair button.

It also prepares the guard: Xray's `RoutingService` (switch routes without a restart),
a local-only socks probe port per proxy outbound (`guard-<outbound>`, 127.0.0.1:10808+,
stable across re-runs), and rule tags `main-<outbound>` / `dns-upstream`.

## tunnel-guard - failover without observatory

Xray's balancer + observatory (and the panel's UI for it) proved unreliable, so this
does the same job from outside, once a minute (systemd timer):

- probes every path with a real HTTPS request through its own probe port
  (one retry, so a single lost SYN is not an outage), plus ping and handshake age
- primary down **2** runs in a row and backup healthy -> users (and the DNS cache's
  DoH) move to the backup through Xray's routing API: **no restart, nobody is
  disconnected**. Back after **3** healthy runs. If x-ui restarts meanwhile, it
  re-applies the move.
- handshake older than 3 min -> restarts `awg-quick@<iface>` (at most every 10 min)
- Telegram alerts (bot token and chat from the panel), sent through whichever path works
- daily report: traffic per tunnel (vnstat), loss/RTT (from `mtunnel-probe` if
  present), outages, top 5 users by traffic, and - on 3.x panels - who expires within
  three days and who is above 90 % of their quota, so renewals are not missed
- warns 20 days before the TLS certificate expires, for certs renewed by hand
- watches the **mux path** separately. `xui-split` sends light traffic through a second
  copy of the primary outbound with mux on, and that copy can stall on its own while the
  tunnel under it is perfectly healthy: one stuck carrier connection freezes every stream
  riding it, so Telegram (routed by IP) and video (routed by domain to the plain outbound)
  keep working while ordinary browsing dies. Any path named `<name>-mux` is probed but
  never failed over to; when it fails twice the `split-light` rule is dropped, light
  traffic falls through to the plain outbound, and it goes back after 3 healthy runs.
  Both moves are Telegram alerts and neither restarts anything.

```bash
cp guard/tunnel-guard.example.json /etc/tunnel-guard.json   # install.sh does this
tunnel-guard --check      # status only
tunnel-guard --test       # Telegram test message
tunnel-guard --report     # daily report now
journalctl -t tunnel-guard
```

**Order `backups` so the same server comes first.** On this hub the primary is a
WireGuard tunnel to a server abroad and `MAIN-tcp` is *the same server* reached
over TCP/443 with Reality. When UDP to that server is throttled - which happened,
and which no port or obfuscation change fixes - the tunnel dies while the TCP path
to the identical machine stays perfect. Failing over to a *different* server also
works, but everything that depends on that particular exit goes with it (a service
that allow-lists the exit IP, a region check, a proxy chained behind it). So `"backups": ["MAIN-tcp", "BACKUP"]` - same
machine over another transport first, a different machine only when the machine
itself is gone. It costs latency (76 ms instead of 40) and keeps everything else
working.

Paths come from `paths` in `/etc/tunnel-guard.json`; the probe port is found by name
(`guard-<path>` in the live config), so the mux path needs nothing but an empty entry:

```json
"MAIN-mux": {}
```

`peer` is optional - leave it out and the ping check is skipped, which is what you want
for a path that shares another path's tunnel.

Needs `xui-split apply` first (probe ports, rule tags, RoutingService).

## awg-hop - when the port is filtered, not the link

A tunnel can stop handshaking while everything about it is healthy. Measured on a
live hub: both daemons up, keys matching, endpoint correct, the hub sending 15 KB
of handshakes in 12 seconds and receiving **zero** bytes back - and a plain UDP
packet to the *same server on a different port* answered immediately, in both
directions. Only the flow on the tunnel's own port was being dropped. Restarting
the interface cannot fix that, because the port is what was identified.

`awg-hop` moves both ends to a new port. The far end goes first, over SSH through
a forced command that can set the port and nothing else, then the local config
and a restart; if the new port does not handshake within 25 s it tries the next,
and if none of them do it puts the original back on both sides - a filtered
tunnel is bad, two ends that disagree are worse.

```bash
bash guard/awg-hop-setup.sh MAIN root@<server-abroad>   # once, on the hub
awg-hop --status
awg-hop MAIN --dry-run
awg-hop MAIN                 # or --port 28731 to choose it yourself
```

The setup script makes a key used only for this and pins it:

```
command="/usr/local/sbin/awg-port",restrict ssh-ed25519 ... hub-awg-hop
```

so a stolen key can change the tunnel's port and get no shell, no forwarding and
no file access. The hub is in Iran; the servers abroad should not become
reachable just because it is. The script refuses to finish if the key turns out
to get a shell.

## awg-over-tcp - when no UDP port survives

Port hopping assumes one port is identified. On some networks it is all of UDP:
measured on a live hub, **any** UDP flow from abroad let exactly six packets in
and then nothing - every port, any rate, even when the hub sent first, even plain
random bytes with no AWG at all. The AWG handshake fits in six packets, so the
tunnel looks alive for a second and then dies; hopping only buys another six.
The way out is to stop sending UDP across the border: `awg-over-tcp.sh` carries
the tunnel inside a TLS WebSocket (wstunnel) over TCP, which is not cut.

```bash
bash guard/awg-over-tcp.sh awg0 root@<server-abroad>              # on the hub
bash guard/awg-over-tcp.sh awg1 root@<server-abroad> awg0         # far end names it awg0
SNI=<domain of that server> bash guard/awg-over-tcp.sh awg0 root@...   # TLS to a bare IP dropped
```

The far end runs `wstunnel server` on tcp/28443 that may forward only to its own
AWG port and only for a client that knows a random path prefix (root-only
`/etc/wstunnel-*.env`); the hub runs `wstunnel client` on `127.0.0.1:510x0` and the
tunnel's `Endpoint` points there. Keys, addresses and AWG settings are untouched.
When the guard restarts a stale tunnel it restarts `wstunnel-<iface>` first.

Then make the tunnel a path the guard can fail over to - any inbound on the far
server, reached at its tunnel address:

```bash
xui-path add MAIN-AWG "vless://<id>@10.0.10.1:443?type=tcp&encryption=<...>#x" --iface awg0
```

(`encryption=` carries VLESS Encryption; `--iface` lets the guard show and watch
the handshake.) Two more things learned on the way, with plain UDP probes:

- a QUIC-looking first packet (`I1`-`I5` = `<b 0xc0000000...>`) got the **whole flow**
  dropped after one packet, and a STUN one lost about half; DNS- and DTLS-looking
  signature packets and random bytes passed. Check the `I` packets before
  blaming anything else.
- TLS to a well-known hosting IP with no SNI stalled at the handshake; the same
  connection with the SNI of a domain pointing at that server went through.

tunnel-guard calls it by itself: a tunnel path whose handshake is stale, which it
has already restarted without effect, and whose siblings are healthy (so the
hub's own internet is not the problem), gets hopped once - then a Telegram line
saying which port it moved to. `hop_after_fails` (default 4) and a 30-minute
cooldown keep it from thrashing.

A caveat worth knowing before reaching for it: hopping helps when the *port* was
identified. It does not help when UDP toward that destination is throttled as a
whole. Telling the two apart takes a minute: send a handful of plain UDP packets
from the far server to the hub on a fresh port pair, in one order and then in the
reverse order. If the same packets survive both times it is the content or the
size; if only the *first two or three* survive whatever they are, the flow itself
is being cut and no port will help - use the TCP path to the same server instead.

## xui-path - a backup path from a share link

A path is three things that have to agree: an outbound in the panel's Xray template, a
local socks port so tunnel-guard can probe it on its own, and an entry in
`/etc/tunnel-guard.json`. Doing that by hand means three files and a restart, so:

```bash
xui-path list
xui-path add TR2 "vless://<uuid>@tr.example.com:443?type=ws&security=tls&path=%2F#TR2"
xui-path remove TR2
```

It parses vless, trojan, vmess and shadowsocks links (tcp/ws/xhttp/grpc/httpupgrade with
none/tls/reality), `hysteria2://` (native in Xray, built the way 3x-ui builds it: salamander
obfs and `mport` hopping become finalmask masks) and `tuic://` (Xray has no TUIC: a local
SOCKS port in sing-box's config carries it, which needs sing-box and restarts it), writes the outbound in the same shape the panel itself uses, picks the
next free probe port from 10808, puts the `guard-<name>` rule next to the other probe
rules, tests the whole config with `xray -test` **before** touching anything, backs up,
restarts x-ui, saves the new baseline for config-guard, and then actually connects
through the new path and tells you whether it answered:

```
مسیر TR2 اضافه شد و جواب داد (HTTP 204 در 0.41 ثانیه)
```

A path that does not answer is still added - the guard simply never moves users onto it
while it is down. The primary cannot be removed, and neither can the path users are on
right now; switch them back first. The same list, with an add form and a remove button
per path, is the first card of the Optimizer's tools tab.

`xui-path prune` drops probe ports whose outbound is gone. Such a port has no rule, so its
traffic falls through to the first outbound and it "answers" for a path that no longer
exists. This happens through Xray's API, so x-ui is not restarted.

## xui-tunnel - one path, several tunnels to the same server

A GRE tunnel and an AWG tunnel to the same server reach the same Xray listener at two
addresses (`10.0.1.2` and `10.0.10.1`). A path therefore does not need to be added once
per tunnel. It keeps a list of addresses, and its outbound moves between them:

```bash
xui-tunnel discover                         # every tunnel, where it lands, which path rides it
xui-tunnel scan MAIN --save              # try MAIN's outbound on every tunnel; list the ones that answer
xui-tunnel add MAIN 10.0.30.1           # one more address by hand (tested, kept even if not up yet)
xui-tunnel set MAIN 10.0.1.2,10.0.10.1 --auto on   # the list, best first, and automatic switching
xui-tunnel use MAIN 10.0.10.1           # move now
xui-tunnel list
```

- **discover** reads the kernel's tunnels. For GRE it takes the remote address. For
  AWG/WireGuard it takes the endpoint, or the `wss://` target of the local wstunnel that
  carries it. For a sing-box tun it follows sing-box's config to the server it goes to.
- **sing-box tuns** are used differently from routed tunnels. The address stays the same
  and the outbound is sent *through* the tun: the entry `10.0.1.2@sb0` sets Xray's
  `sendThrough` to the tun's own address, the hub's policy routing sends that into the tun,
  sing-box carries it to its server, and the connection lands on the same listener
  (measured: MAIN over `sb0` 486 ms, BACKUP over `sb1` 314 ms). While a path is on
  such an entry, the guard times the round trip through that tun.
- **scan** does not rely on that guess. It starts the path's own outbound in a throwaway
  Xray, once per tunnel address and in parallel, and keeps the ones that fetch a page.
  This finds tunnels whose far end is unknown, and it showed that a Reality path on a
  public domain also answers on the main server's tunnels. `xui-path add` runs a scan with
  `--save` right after it adds a path, and also tries any `--addrs A,B` given with it.
- **use** swaps the outbound and its `-mux` twin through Xray's API (`rmo` + `ado`).
  x-ui is not restarted, and only the connections on the old tunnel drop. It also updates
  the template and tells the guard to ping the new peer. If the outbound is Xray's first
  (default) one, x-ui is restarted instead, because `ado` appends at the end.
- **auto** is what `xui-tunnel.timer` runs every minute:
  - Paths that have `--auto on` are checked through their probe port.
  - Two failures three seconds apart move the path to the next address that answers,
    with a Telegram line.
  - After three healthy checks of a better address (and at least ten minutes after the
    last move), the path goes back to it.
  - Once an hour, every path is rescanned, and new addresses show up as suggestions on
    the page.

State is kept in `/var/lib/tunnel-guard/tunnels.json`, and the lists are kept under
`"tunnels"` in `/etc/tunnel-guard.json`. On the page this is the **Tunnels** card in
tools. It has a find-tunnels table, and each path has its addresses, test / use / ▲ /
remove buttons, a scan button, a field for typing an address, and the last moves.


### Several tunnels at once

Instead of one tunnel at a time, a path's new connections are shared between two or more of
its tunnels, so the traffic is never all on one of them. All tunnels of a path reach the same
server, so the user's exit IP does not change.

- **smart** (default): every healthy tunnel takes connections; one that loses over a third of
  its pings or answers slower than 1.5 s leaves the mix until it recovers (Xray `leastLoad`).
- **random** / **roundrobin**: by weight (1x-3x; a 2x tunnel gets twice the new connections).
- Priority is the path's tunnel order; the first is the fallback when none answers. Xray's
  `burstObservatory` skips a dead tunnel in every mode; the timer tests each tunnel every 5
  minutes and tells Telegram when one drops out or comes back.
- **Pin** (runtime, no restart): all of the path on one tunnel until unpinned or x-ui restarts.
- The page shows each tunnel's share of the last hour's traffic.

```bash
xui-tunnel mix MAIN on 10.0.1.2,10.0.10.1 --how smart
xui-tunnel mix MAIN on 10.0.1.2,10.0.10.1,10.0.1.2@sb0 --how random --weights 2,1,1
xui-tunnel mix MAIN pin 10.0.10.1     # pin none to let go
xui-tunnel mix MAIN off
```

Turning it on, changing it or off restarts x-ui once, after an Xray test and a backup; the path
is then checked through its own probe port and everything is put back if it does not answer.
How: one copy of the path's outbound per tunnel (`MAIN~1`, `MAIN~2`, `MAIN~1b` for a
2x weight), a balancer `mix-MAIN` over them, and every rule that went to `MAIN` (users,
DNS, the guard's probe) now names the balancer. `MAIN` itself stays, unused, as the model of
the copies, so tunnel-guard failover, xui-split (which re-applies the mix after every rebuild),
xui-optimizer and config-guard (which now also keeps balancers and the observatory) keep
working. `MAIN-mux` is mixed the same way. A loopback outbound in front of the balancer would
have left the rules alone, but Xray counts loopback traffic against the same user a second time
and quotas would run out twice as fast. Shared code: `lib/xuimix.py`.

## xui-reach, xui-geo, xui-cert

- **xui-reach** - every probe the hub runs goes outwards, but a user's outage is often on the
  path from their operator to the hub. Every 10 minutes (`xui-reach.timer`) it asks
  check-host.net's nodes inside Iran (Tehran, Isfahan, Shiraz, Qom; different networks) for a
  real TCP connect to every public port and the subscription port, keeps 48 hours, and tells
  Telegram when a port is unreachable from most of them twice in a row, and when it is back.
  The nodes are hosting networks, not the mobile operators. The hub's address is sent to
  check-host.net. Settings in `/etc/xui-reach.json` (`host`, `ports`, `nodes`, `fails_to_alert`).
- **xui-geo** - `ir-direct` rules trust `geosite_IR.dat` / `geoip_IR.dat` (Chocolate4U/
  Iran-v2ray-rules), which the panel only replaces on its own update. `xui-geo update` fetches
  the newest release (direct, or through the tunnels), checks sha256, test-loads them with Xray
  and swaps them in; `--restart` restarts x-ui only if something changed; `xui-geo.timer` runs
  it weekly at night. `xui-geo security on|off` blocks the malware, phishing and cryptominers
  lists for every user inbound (one tagged rule).
- **xui-cert** - a certificate issued with plain `--dns` cannot renew itself. `xui-cert auto
  DOMAIN --dns dns_arvan` (or `dns_cf`, ...) with the provider's token exported once re-issues it
  through the DNS API and installs it where the panel reads it; acme.sh's cron renews from then
  on. `--test` proves the token against Let's Encrypt staging first. `xui-cert status` lists the
  certificates the panel, the subscription and nginx use and how each renews.
- The page's check-up also opens a real subscription link as a browser (3x-ui 3.3+ serves a
  page: usage ring, quota, one-tap import, RTL) and as an app, and checks its certificate.

## xui-subpage - a Persian subscription page

3x-ui keeps the subscription page's language apart from the panel's (a `subLang` cookie in the
user's browser) and defaults it to the phone's language, so an English phone gets English. It can
also render the page from a template (Settings -> Subscription -> Sub Theme Directory).
`xui-subpage on` builds that template out of the panel's own page, taken from the x-ui binary, so
it looks exactly the same, and adds only: Persian chosen once per browser (the page's language
button still works), Jalali dates while the page is Persian, and a red stamp across a subscription
that has **expired**, **run out of data** or been disabled, refreshed every 30 s. The page's asset
names change with each panel release, so `xui-subpage.path` rebuilds it when the x-ui binary
changes. `on` renders a real subscription through the panel and restores the previous setting if
the template is not served; `off` returns the untouched page; `status` also says whether the
template matches the installed panel. No restart: the setting is read on every request.
The database is detected the way x-ui sees it (the running process's environment, then the
service's Environment= / EnvironmentFile=, then SQLite in /etc/x-ui), so panels on PostgreSQL work
too (needs psql); `xui-subpage detect` shows it. `on` takes `--no-jalali`, `--no-stamp`, `--always`
and `--sub LINK`. The same tool is published on its own as
[3x-ui-sub-fa](https://github.com/MaxTeller95/3x-ui-sub-fa).

## config-guard - the routing comes back by itself

Saving an inbound from a panel tab that was opened *before* the last change makes x-ui
write an older Xray template. It happened twice here: the DNS cache, the traffic split
and hand-written `ir-direct` rules disappeared in one click, and nothing said so - users
just started going out the wrong way.

config-guard keeps a baseline of every routing rule that carries a ruleTag, of the proxy
outbounds, of the guard's probe inbounds and of the `dns` block. systemd runs it the
moment `config.json` is written (a `.path` unit, ~30 ms) and every 10 minutes as a safety
net. When something in the baseline is gone it puts it back **in its original position**,
tests the result with `xray -test`, backs up, saves it into the panel's template and
restarts x-ui - then says on Telegram what was missing and that it restored it.

```bash
config-guard              # check and print, change nothing
config-guard --fix        # what systemd runs
config-guard --save       # you removed a rule on purpose: accept the new state
```

New rules you add are absorbed into the baseline on the next clean run, so only
*disappearances* are acted on. Two repairs in a row inside 10 minutes stop at an alert
instead (something is re-saving a stale config, and restarting in a loop would be worse).

## xui-probe-client - "is it me or them?"

The hub builds the same client config the user's app gets from the subscription - same
UUID, same transport, same address - runs it against its own public port and measures it:

```bash
xui-probe-client alice
  in-80-tcp    hub.example.com:80   ws/none     ->  HTTP 204 in 231 ms (connect 1 ms), 161.4 Mbit/s
  in-443-tcp   hub.example.com:443  xhttp/tls   ->  HTTP 204 in 299 ms (connect 9 ms), 140.8 Mbit/s
```

Nothing is created or changed in the panel and the UUID is never printed. On the
Optimizer page the same thing runs from a box in the users tab, and clicking any user's
name in the tables starts it.

**Xray 26 refuses plain VLESS (no TLS) towards a public address**, so for such an inbound
the probe dials the listener directly - the transport, the path and the UUID are still
exercised, the hop from the internet is not. That refusal is on the *client* side, which
means a user whose app ships a 26.x core cannot use a plain-VLESS config at all; the
answer is TLS, Reality, or VLESS Encryption on that inbound.

## path-score - is this IP worth buying?

Run it on the hub against the IP a provider is offering, before you pay. It
measures the transport from *this* hub and reads the network's routing record,
then says what it found.

```bash
path-score 198.51.100.50
path-score new.example.com --json
```

```
transport, from this hub
  ICMP                   1.0% loss
  TCP :443               30/30 connected              good
  TCP rtt                p50 40.1  p95 42.3 ms        jitter 1.4
  vs BACKUP              41.2 ms                      candidate is -3.2 ms
the network
  ASN                    AS49805                      ANKSOFT Berke FINCANCI
  transit upstreams      1                            poor  single-homed
routing stability, last 30 days
  BGP withdrawals        2792                         poor  expect short dropouts
```

Two things it exists to stop you doing:

- **Trusting `ping`.** Networks rate-limit ICMP, so a host can show 5 % "loss"
  and carry TCP perfectly - the current BACKUP path does exactly that. The
  reverse is worse: a flawless ping on a prefix that gets withdrawn from the
  global table a hundred times a day. So it also opens 30 real TCP connections
  and reads the prefix's 30-day BGP churn from RIPE. Withdrawals are what a user
  feels as a few seconds of nothing.
- **Reading latency as speed.** On this hub the 75 ms path out-runs the 39 ms
  one, because the ceiling is the far server's uplink, not the distance.
  path-score deliberately measures no throughput and tells you to buy one month
  and measure instead.

It needs internet access, which it takes through a `guard-*` probe port if
`xui-split` has made one. Nothing is changed by running it.

## migrate - from a 2.x panel to 3.x

In 3x-ui 2.x each inbound owns its clients, so putting one user on two inbounds meant a
second client with a second email - and the panel then counted its traffic separately
and reported `total=0` (unlimited) in the subscription header. 3.x keeps clients in
their own table, linked to inbounds, with one quota, one expiry and one usage counter.

```bash
bash migrate/upgrade-2x-to-3x.sh     # backup, install, verify (inbound tags are kept)
migrate/purge-twins                  # show; then: migrate/purge-twins apply
tools/xui-fix-inbound-lists          # show; then: apply
```

`purge-twins` folds every duplicate client back into the original - traffic included -
and, importantly, also clears the copy of it **inside the inbound**; the panel re-creates
clients from that copy when an inbound is saved, so leaving it behind brings them back.
`xui-fix-inbound-lists` then makes each inbound list exactly the clients linked to it,
which is what the panel needs to save a client without "empty client ID".

Measured on a live hub: 350 client entries became 187 real clients, 161 of them on
several inbounds, each with a single traffic row; quota in the subscription header went
from `total=0` back to the real value, and traffic through both configs landed in one
counter.

## dash - the Optimizer page inside the panel

An **Optimizer** item in the panel's left menu (above Log Out) opens a page whose top
strip always shows what users are getting right now - download, upload, how many are
online and which path they are on - and four tabs below it. It follows the panel's
light/dark theme and works on a phone.

**نمای کلی** - throughput of the whole hub over the last 3 h (per minute) or 24 h (per
hour), with the number of online users drawn over it; how the last hour split between
the paths; and which inbound (which config the users hold) is carrying the traffic at
this second. Then the paths themselves: health, RTT sparkline, 24 h availability, and
the **keep users here** button per path, which the guard obeys until you switch back
to automatic.

**کاربران** - who is downloading right now and how fast, who runs out of days within a
week, who is above 85 % of their quota, who is connected from several addresses at once
(the panel's own `limit IP` is the switch for that), and who has not shown up for a
month. Enough to do the renewals without opening the client list.
It also shows the real speed users reach per inbound: each user's fastest 5-second upload
and download in the last 24 hours (from Xray's stats; samples under 256 KB do not count),
as median, 90th percentile and maximum per inbound - so two transports (WebSocket on 80,
TLS on 443, ...) can be compared on the users' own networks.

**سلامت** - the **چکاپ** button runs thirteen checks in about ten seconds: the system
clock (TLS and Reality fail on a wrong clock), `xray -test`, each path through its own
probe port, a real 25 MB download to measure the speed the hub can actually reach, the
DNS cache, the tunnel's largest unfragmented packet against the configured MTU, memory,
disk, load, conntrack, the certificate, the panel's database and the kernel settings -
and scores the result. Below it, **بازرسی پنل** checks the invariants this panel has to
keep (every client linked to an inbound is in that inbound's own list, `flow` in the
link matches the listener, no orphan traffic rows, no duplicate emails or UUIDs, rule
order, `remarkTemplate`) and offers a **تعمیر کن** button for the ones that are
repairable. Then server resources, this month's traffic per tunnel, the certificate and
recent outages.

**ابزارها** - the operator console, so routine work does not need ssh:

- **paths** - the failover chain, with an add form that takes a share link (`xui-path`)
  and a remove button per backup; the primary and the path in use cannot be removed.
- **services** - x-ui, nginx, xo-dash, both guards and each `awg-quick@` tunnel, with
  state, uptime and memory, and a restart button per unit. Only that list can be
  restarted; anything else the page is asked to restart is refused.
- **backups** - take one now, download a backup's database to your own machine (the only
  copy that survives losing the server), restore one (it backs the current state up
  first, then restarts x-ui), and prune everything older than a week except the last ten.
- **"does this address open through which path?"** - types a domain, opens it from the
  hub through every probe port and shows each path's HTTP code and time. A 403 there is
  usually the site blocking the exit IP, not the path being broken.
- **logs** - tunnel-guard, config-guard, x-ui, the users' access log, nginx, this page.
- **mux-bench** - runs the three-minute benchmark as a background job and polls for the
  result, because a three-minute request would time out.
- versions and uptime.

Numbers come from Xray's own stats API, sampled every 5 s and kept as one bucket per
minute for 24 h in `/var/lib/xo-dash/history.json`, so the page never queries the
panel's database for traffic and the graph survives a restart of the page service.

The panel binary is not modified, so panel updates do not remove it: nginx serves
the panel and adds one `<script>` to its pages that inserts the menu item. The page
itself is a small service on 127.0.0.1 that only answers requests carrying a live
panel login (it replays the browser's panel cookie to the panel), so there is no
second password.

```bash
bash dash/install-dash.sh 59085          # try it on a second port; the panel keeps its own
bash dash/install-dash.sh <panel port> move   # final: x-ui moves to 127.0.0.1, nginx takes its port
```

If you later change the panel's port, base path or certificate in Panel Settings,
run the installer again. Keep "Listen IP" = 127.0.0.1 in Panel Settings after `move`.

## abroad-tune - IPv4 first on the way out

With the freedom outbound's default, some IPv6 paths connect and then stall:
**www.microsoft.com timed out after 15 s** through an exit abroad, while IPv4 answered
in 0.17 s. `abroad-tune` sets `UseIPv4v6` (IPv4 first, IPv6 as fallback) in the
panel's template, in the right place for the Xray version.

```bash
abroad-tune          # test
abroad-tune apply    # backup, apply, restart x-ui
```

## sysctl/99-proxy-tcp.conf

`tcp_slow_start_after_idle=0`, `tcp_notsent_lowat=16384`, `tcp_mtu_probing=1`.
Applied live, no restart.

**TCP Fast Open is left out on purpose:** Xray 26.x does not enable it on its listening
socket even with `tcpFastOpen` set (the kernel issues cookies to a plain test listener
on the same host, Xray's never does), so the hub's counters showed 0 successful and
30 956 failed TFO attempts. Mux is the working way to skip the handshake.

## Why not a real cache, like Squid?

Almost everything is HTTPS, and inside the proxy it is encrypted a second time, so
the hub never sees two identical responses it could cache. Making it see them means
installing your own root CA on every user's device - which breaks apps that pin
certificates (Telegram, WhatsApp, banking) and hands the hub every password.

## Panel settings worth knowing (3.x)

**Panel Settings -> Subscription -> Information -> Remark Template** decides what each
config is called in the user's app. Its default, `{{INBOUND}}-{{EMAIL}}|📊 {{TRAFFIC_LEFT}}|⏳ {{DAYS_LEFT}}D`,
is not stored in the database until you save it, so changing `remarkModel` or turning
off `subShowInfo` has no effect while it is there. `{{INBOUND}}` alone gives the plain
inbound name; quota and expiry still reach the app through the `Subscription-Userinfo`
header. The Optimizer's panel linter warns when this setting is empty.
