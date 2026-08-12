# OS premium data and mapgen: the licensing decision

Recorded 2026-08-10, from two independent verification passes with
adversarial checking (six research agents, six refutation agents, all
reading OS's operative contract text rather than marketing pages).

This document exists so the question is not reopened from memory. A
prior pass on 2026-08-05 reached the same conclusion on weaker grounds
and recorded it only in a gitignored ledger, which is why it was
relitigated. The grounds below are clause level.

## The decision

**mapgen remains an open-data tool. No OS premium byte enters a
package, a fusion, a cache, or any client-receivable output. The
derived-data firewall stays exactly as written.**

The one permitted use of premium data is the `mapgen benchmark`
command: read in memory, compute aggregate statistics, write the
report, discard. No geometry, no feature identifier, no attribute
value beyond class names reaches any output.

## Why, in one sentence

The premium grant is a licence to VIEW data for the duration of a
request, and mapgen's entire product is a folder of files that
persists.

## The clause evidence

- **Appendix 2 para 1.1.1(i)**: an End User receives "a non-exclusive,
  non-transferable licence to View Premium API Data in your Products
  and / or Services for the duration of the Premium API Transaction",
  plus permission to "temporarily cache it for up to 24 hours". There
  is no storage right in the grant at all.
- **"Caching" is defined** as "the automatic, immediate download and
  temporary storage of data as an integral and essential part of a
  technological process". A dated survey package opened in Rhino next
  month is not that, by definition rather than by argument.
- **Appendix 2 para 2.1.7**: "You shall ensure that your End Users are
  unable to download, store or extract the Premium API Data except to
  the extent otherwise provided for in this Agreement." Retention is
  not merely unpermitted, preventing it is an obligation.
- **Clause 4.1.2a)** grants a Partner only three things: develop,
  evaluate and test your Products; the OS Places trial; and provide
  your Products by sub-licensing to End Users. **Clause 4.2.4** sweeps
  up any "purpose not expressly permitted by this Agreement".
- **Eligibility**: OS states the Premium Plan "is for OS Partners
  building an app or service for use by a third party", and
  "Organisations that build apps or services only for their own
  internal business use ... are not an OS Partner", directing such
  users to "our OS OpenData Plan or contact one of our OS Partners".
- **Planning applications**, Appendix 2 para 2.1.8: printing is
  permitted only "for their personal non-commercial use", and "the use
  of prints for the purposes of planning permission applications is not
  permitted unless your End User is also party to an OS licence that
  does permit such use". A practice is a Business End User, so it has
  no printing right under this clause at all. For a digital submission
  the operative bar is 2.1.7, not 2.1.8.
- **No tracing**: Appendix 2 para 2.1.2, "You may not digitise features
  or symbols from any of our Datasets."

**OS has stated the rule in plain English for exactly this product
shape.** Writing about Cadcorp SIS Desktop, a desktop GIS with the same
persistence question mapgen has: "Premium Data products can only be
cached locally for the duration of a SIS Desktop session. When
requesting OS Open Data, SIS Desktop allows the requested data to be
imported, breaking the connection to the OS Features API, e.g. to use
the data offline." Premium is session only. Open data may be imported
and kept.

## Models considered and rejected

- **Software only, user brings their own key.** Rejected. The user's
  own grant is development and testing, not production use, and a free
  public release is not among clause 4.1's permitted distribution
  routes. OS visibly tolerates bring-your-own-key tools (a GPL QGIS
  plugin, a WordPress plugin OS blogged about approvingly), but none of
  them persists premium data, and tolerance is not permission.
- **Hosted service on our key.** Rejected for packages. Viable in
  principle for on-screen viewing, but para 2.1.7 still forbids the
  download, and the compliance stack is heavy: a written agreement
  executed by every End User, a maintained register of End Users
  disclosable to OS, OS logo and errors tool link, plus uncapped spend.
- **Internal practice use for client work and planning.** Rejected
  twice over: excluded from the Premium Plan by eligibility, and the
  deliverable is named in the planning clause.
- **Bundling premium files.** Rejected outright by clause 4.2.1.

## The legitimate route for a planning drawing

Buy a per-site licensed plan from an OS Mapping and Data Centre or a
Planning Portal approved provider. It arrives at 1:1250 or 1:2500
pre-stamped with the OS licence number the authority checks, which is
precisely the condition para 2.1.8 requires. Bill it to the client as
a named disbursement. Nothing pulled through the Data Hub API can
satisfy that condition, because the API issues no licence number.

## Pricing, for the record

- OS NGD API Features: **£0.19 per request ex VAT**, one request
  returning **up to 100 features** (hard cap). One HTTP request is one
  transaction whatever it returns.
- The £1,000 monthly free allowance therefore buys **5,263 requests**,
  a figure OS publishes itself. About 526,300 features at full pages.
- **Development mode does not consume the allowance at all.** Its
  limits are 50 transactions per minute per API per project, a
  three-month maximum, and trial and testing purposes only.
- **There is no hard stop at £1,000.** Overage is billed at month end.
  The only published mechanism that halts spend is having no payment
  card on file. Keep the card off.
- The £1,000 is not contractual: clause 6.9 lets OS reduce the
  threshold on 30 days' notice.

## Unresolved, and worth an email

Whether a practice intending a public software release qualifies as an
OS Partner at all is the upstream question, and only OS can answer it.
Ask commercialenquiries@os.uk in writing, and keep the reply. Ask also
whether development-mode use to compute aggregate quality statistics
about open data, where no OS byte reaches any output, sits within the
development-mode grant.

Contract text was read through rendering proxies because OS's legal
pages are JavaScript applications that return an empty shell to direct
fetching. It was confirmed through two independent routes, but no human
has read it in a browser. The live pages are
`osdatahub.os.uk/support/legal/api-terms` and
`osdatahub.os.uk/support/legal/datahub-terms`.
