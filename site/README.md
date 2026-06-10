# Marketing site (lulurobot)

Static Astro site served at `www.z1nexusn1.org` and the apex `z1nexusn1.org`,
on Cloudflare Pages. Pure HTML/CSS + ~15 lines of JS that fetches the current
version string from `downloads.z1nexusn1.org/version.json`.

## Local

```
cd site
npm install
npm run dev      # http://localhost:4321
npm run build    # → dist/
```

## Architecture

```
www.z1nexusn1.org           → Cloudflare Pages (this site)
z1nexusn1.org (apex)        → same Pages deployment (redirect / mirror)
downloads.z1nexusn1.org     → Cloudflare R2 public bucket
   ├── lulurobot-mac-vX.Y.Z.zip
   ├── lulurobot-win-vX.Y.Z.zip
   ├── lulurobot-mac-latest.zip   ← site links here
   ├── lulurobot-win-latest.zip   ← site links here
   └── version.json               ← read at runtime to show current version
bridge.z1nexusn1.org/*      → Cloudflare Worker (already deployed)
```

## Release flow

A tag push (`git tag v0.3.0 && git push origin v0.3.0`) triggers
`.github/workflows/build.yml`:

1. `build-mac` + `build-win` produce the artifacts.
2. `publish-r2-pages` (gated on tag) downloads both, uploads each twice —
   versioned and `-latest` alias — and writes a fresh `version.json` to R2.
3. Same job builds this site and `wrangler pages deploy`s it.

## One-time operator setup

### 1. Enable R2 in the Cloudflare dashboard

R2 has a one-click ToS gate the CLI can't bypass. Open:

```
https://dash.cloudflare.com/<your-account-id>/r2/overview
```

Click **Enable R2**, accept the terms. Free tier covers 10 GB / 1M reads/mo.
No payment method required.

### 2. Create the bucket + bind the custom domain

```bash
cd worker
npx wrangler r2 bucket create phantom-click-downloads
npx wrangler r2 bucket domain add phantom-click-downloads downloads.z1nexusn1.org
```

(The domain bind tells Cloudflare to serve the bucket publicly under
`https://downloads.z1nexusn1.org/<key>`. Cloudflare auto-creates the DNS
record under the zone you already own.)

### 3. Create the Pages project + bind www + apex

```bash
cd site
npx wrangler pages project create lulurobot-site --production-branch=main
# First-time deploy (creates the project so the CI deploy step has somewhere
# to push to). Subsequent deploys overwrite this.
npm install && npm run build
npx wrangler pages deploy dist --project-name=lulurobot-site --branch=main

# Bind your domains. Both go to the same project — `www.` is canonical,
# apex is convenience.
npx wrangler pages project domain add lulurobot-site www.z1nexusn1.org
npx wrangler pages project domain add lulurobot-site z1nexusn1.org
```

### 4. Create a CF API token for CI

The GitHub Actions workflow needs a Cloudflare API token to push to R2 and
Pages on every release. Tokens are easier to scope than your account
password.

`https://dash.cloudflare.com/profile/api-tokens` → **Create Token** →
**Custom token** with these permissions:

- Account → Workers R2 Storage → **Edit**
- Account → Cloudflare Pages → **Edit**
- Account → Account Settings → **Read**

Account resources: `Include → <your account>`. Zone resources: not needed.

Copy the token (only shown once) and set it on the repo:

```bash
gh secret set CLOUDFLARE_API_TOKEN --repo todanley/PC
# paste the token, ↵

gh secret set CLOUDFLARE_ACCOUNT_ID --body '<your-account-id>' --repo todanley/PC
```

After that, every `git push origin vX.Y.Z` reaches CN users via R2 + Pages
within ~2 minutes of CI finishing.

## Why R2 + Pages and not GitHub Releases

GitHub Releases is free and works for non-CN audiences, but `github.com` and
its release-asset CDN are intermittently blocked or throttled from the
mainland. The bridge subdomain has already been verified 0% blocked on 17ce
and GreatFire from inside CN — `www.` and `downloads.` ride the same anycast
edge, same routing, same firewall behavior. Don't ship CN distribution
through GitHub.
