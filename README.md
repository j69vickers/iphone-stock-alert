# iPhone pickup alert: Apple Barton Creek (Austin, TX)

Checks Apple's `fulfillment-messages` endpoint every 5 min via GitHub Actions and pushes to your iPhone (ntfy) when a watched part flips **unavailable -> available**.

## Layout
```
iphone-alert/
├─ check.py                      # the checker (stdlib only)
├─ config.json                   # zip, store name filter, list of part numbers
├─ state.json                    # last-known status (auto-committed by the workflow)
├─ README.md
└─ .github/workflows/stock.yml   # cron */5 + state commit
```
Push the **contents of `iphone-alert/`** as the root of a new GitHub repo (the `.github` folder must be at the repo root).

## Setup
1. **ntfy on iPhone**: App Store -> "ntfy" -> open -> **+** -> subscribe to a topic. Pick a long unguessable name (topics are public-by-name), e.g. `josh-iphone-7f3k9x2q`. In Settings, allow notifications (and Critical Alerts if offered, so urgent alerts break through Focus).
2. **GitHub repo**: create a repo (private is fine; Actions minutes are free for public repos, 2,000/month for private. A 5-min cron uses roughly 1 min per run, so **use a public repo** or you'll exhaust the private allowance. The topic stays secret in Secrets regardless).
3. **Secret**: repo -> Settings -> Secrets and variables -> Actions -> New secret: `NTFY_TOPIC` = your topic name.
4. **Find your part number(s)** (see below), put them in `config.json`.
5. Commit and push. Go to Actions -> *iphone-stock-check* -> **Run workflow** to test. Check the log shows `PART: unavailable` (or AVAILABLE).
6. Optional sanity push: locally `NTFY_TOPIC=yourtopic python check.py --test-notify`.

## Finding the part number
Part numbers look like `MYXX3LL/A` (the `LL/A` suffix = US).
- **Easiest:** apple.com/shop/buy-iphone -> pick model/storage/color -> choose "Pickup: Check availability at another store" -> in Chrome DevTools -> Network, filter `fulfillment-messages`; the request URL contains `parts.0=XXXXXLL/A`.
- Or on the buy page, view source and search for `LL/A` near your selected options.
- Or the Apple Store app: product -> Share, or check the order/reserve URL (`partNumber=`).

**Verify it** (prints Apple's product title for that part, so you know the color/storage is right):
```
python check.py --probe MYXX3LL/A
```
Add each color/config as another entry in `config.json -> parts`. All parts go in a single request.

## How it behaves
- Alerts **only on the transition** to available. State is committed back to the repo only when something changes.
- Failing endpoint: after 6 consecutive failures (~30 min) you get one "Stock checker is failing" alert, and a "recovered" push afterward.
- Response format changed / store or part not found / HTML instead of JSON: alert after 2 consecutive runs.
- 429/5xx: retries with backoff, honoring `Retry-After`.

## Caveats (be honest with yourself)
- This is an **unofficial** Apple endpoint; it can change or block GitHub's datacenter IPs (the schema/failure alerts will tell you). If blocked, run the same script from a home machine/Raspberry Pi via cron instead.
- GitHub cron is best-effort: runs can be delayed by several minutes, and scheduled workflows in repos with no activity for 60 days get disabled (state commits count as activity, but re-enable if it stops).
- The reserve deep link is built from the store number Apple returns; the exact reserve URL format is not documented, so if it doesn't open the right page, the notification still tells you what's in stock. Edit `reserve_link()` in `check.py`.
- Not yet executed against Apple's live endpoint. Run `--probe` and the manual workflow first.
