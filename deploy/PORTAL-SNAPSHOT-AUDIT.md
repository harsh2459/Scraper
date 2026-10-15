# Saved eRegister request and format audit

Inspected the HTML and portal-owned scripts in `D:\tmsearch` as local text and parsed HTML. No saved JavaScript was executed and no portal requests were sent. Original pages remain unchanged. Application/opposition identifiers, personal details and session-token values are not copied into this report or test fixtures.

This refines [IMPLEMENTATION-PLAN.md](IMPLEMENTATION-PLAN.md) and the source analysis `D:\tmsearch\OPPOSITION-AND-COMPLIANCE-FLOW.md`. It verifies saved formats, not current live server behavior or legal deadlines.

## Request contracts observed in scripts

Base URL: `https://tmrsearch.ipindia.gov.in/estatus/`.

| Action | Method and path | Sent data | Response/use observed |
|---|---|---|---|
| Send OTP | POST `Otp/SendOtp` | JSON: `Email`, `Mobile`, `CaptchaAnswer` | Login flow |
| Verify OTP | POST `Otp/VerifyOtp` | JSON: `Email`, `Mobile`, `OtpCode` | Starts tab session |
| Public key | GET `Crypto/GetPublicKey` | None shown | RSA key for encrypting lookup input |
| Store lookup number | POST `TradeMarkApplication/StoreSessionDataEncrypted` | JSON: `encryptedData` | Stores encrypted application number in the session |
| Validate lookup captcha | POST `TradeMarkApplication/ValidateCaptchaAjax` | JSON: `encryptedData` | Validates separately encrypted captcha |
| Track search click | POST `Home/TrackClick` | JSON: `tabToken` | Search/login state check |
| Lookup form | POST `TradeMarkApplication/ViewRegistration` | Form fields include `CaptchModel.CaptchaAnswer`, `AntiforgeryToken`, `__Invariant` | HTML results |
| PR history | POST `RegisteredTM/GetPRDetails` | Form field `token` | HTML fragment |
| Application details | POST `RegisteredTM/Details` | Form field `token` | HTML fragment |
| Document list | POST `RegisteredTM/GetDocumentsDetails` | Form field `token` | HTML document list |
| Reminder list | POST `RegisteredTM/GetReminders` | Form field `token` | HTML table |
| Correspondence list | POST `RegisteredTM/GetCorrespondence` | Form field `token` | HTML table |
| Resolve normal reminder link | GET `RegisteredTM/GetEncryptedRemUrl` | Query `corresNum` | URL string opened by the portal script |
| Opposition record | Link to `RegisteredTM/ShowOppRecDetails` | Query `ID`, `typ` | Saved record HTML; observed type is `O` |
| Opposition reminders | Link to `RegisteredTM/Get_Opposition_Reminder_Correspound` | Query `ID` | Saved reminder HTML |
| Opposition correspondence document | Link to `RegisteredTM/ShowCorrespondence` | Query `CRS_NO` | Notice document |
| Opposition reminder document | Link to `RegisteredTM/ShowReminderCorrespondence` | Query `CRS_NO` | Notice document |
| Examination report | Link to `RegisteredTM/Examreport` | Query `APPLICATION_NUMBER` | Document link; standalone objection-report sample absent |

Portal panel AJAX supplies `RequestVerificationToken` by reading `input[name="__RequestVerificationToken"]`. Global AJAX supplies `Tab-Token` from browser session storage. The login script uses a 60-second keepalive beacon; the saved lookup/result pages also contain a differently cased `Otp/KeepAlive` fetch. Preserve the existing working session flow rather than guessing that all routes accept identical payloads.

The saved lookup/result forms contain `AntiforgeryToken`; the panel script selects `__RequestVerificationToken`. This discrepancy needs a targeted live request trace before changing authentication headers. A saved page is not enough to conclude the server accepts either name interchangeably. Do not reuse old captured token values.

## HTML structures confirmed

- Result table: ten columns covering number, application date, class, filing mode, mark, type, use detail, publication, validity and proprietor. Status/sub-status appear outside that table.
- Application details opposition-discovery table: `Date`, `Opp. Number`, `Opponent Code`, `Opponent Name & Address`, `Details`, `Reminder`. Its links contain opaque `ID` values; follow links from the fresh page rather than building them from the visible opposition number.
- Opposition record: labeled two-cell rows for opposition reference/date, opponent, opponent's agent, status and decision; correspondence is in a nested child table.
- Opposition correspondence header uses `Sr No.` / `Corresp. No.` / `Corresp. Date`; opposition reminders use `S.No` / `Corres. No` / `Corres. Date`. Headers are ordinary `td` cells. `th`-only detection misses them, and traversing all descendant rows duplicates records through outer layout tables.
- Normal reminder script uses an onclick handler plus URL-resolution call. Saved opposition reminders instead link directly to `ShowReminderCorrespondence?CRS_NO=...`. Keep both locator forms, with an explicit source/proceeding namespace.
- Document hrefs are root-relative and include `/estatus/`. Joining them by appending to a base already ending in `/estatus` produces a doubled path. Resolve root-relative, portal-relative and absolute URLs explicitly.
- The saved document titled “Hearing Notice” contains acceptance/advertisement text, not a scheduled hearing date. Document titles and subjects are insufficient to establish event type or event date.

## Changes made from these findings

1. Added `shared-scraper/portal_formats.py`, a pure parser module independent of database configuration. It handles opposition discovery, opposition record fields, both notice-header variants, direct row ownership and both reminder locator forms.
2. Existing normal reminder/correspondence parsing uses that table parser. The worker downloads href-based reminders through its direct document path. Opposition notices stay in their own proceeding namespace in the ledger/events, separate from normal reminder/correspondence tables.
3. Corrected correspondence URL resolution so `/estatus/...` links are not doubled. Document fetching stays on the HTTPS portal origin.
4. Fleet publication now retains notice serial/reference numbers, correspondence/despatch dates, despatch numbers and document locators. It upserts by `(app_no,corres_no)` while preserving the existing source-row ID, so per-user compliance statuses remain attached on repeat refreshes.
5. Uploaded-document artifacts now retain description/date and their portal locator in metadata. Encrypted locator stability across sessions is unverified; it must not be assumed to be a permanent document ID.
6. Added sanitized fixtures under `shared-scraper/tests/fixtures/portal/` and offline parser/worker/publication tests. The additional multiple-proceeding case is synthetic, not a verified second live example.

Those were the initial parser-only changes. The subsequent approved implementation now includes opposition network/storage, source-linked document events, private receipt/confirmation workflows, canonical field merging and incremental fetch depth. See the implementation status in [IMPLEMENTATION-PLAN.md](IMPLEMENTATION-PLAN.md). Direct reinspection confirms that the saved acceptance body overrides its hearing title, and the saved opposition notice yields a two-month period anchored to receipt. The legal-only period sentence is a fixture; personal data/session values remain excluded.

## Optimization contract for the next workstream

Scheduling decides **when** to fetch; fetch-depth policy decides **what** to download.

On each due check, retrieve the result row, application details needed to discover proceedings, and lightweight document/reminder/correspondence lists. Status alone cannot tell whether an opposed application has new notices. Visit discovered opposition record/reminder lists as required by their own fingerprints and safety interval.

Compare stable notice IDs with their proceeding/source namespace, document description/date and verified identity, status/sub-status, publication, validity and opposition membership. Compare meaningful metadata as well as ID sets: the same ID may receive changed metadata. Do not fingerprint rotating encrypted links. Counts alone cannot detect one row replacing another.

Download missing/new documents. Reuse a local artifact only when identity, checksum, completeness and the requested freshness permit it. Re-extract saved PDFs without a portal fetch. Keep a per-document ledger and explicit per-stage results, including listed/downloaded/parsed/review-needed/failed states. Retry missing stages and schedule periodic full checks even when fingerprints match. Empty lists and login/error HTML must not silently prove completion.

Refresh rules can use status, sub-status and proceeding stage with explicit precedence. Stage-based rules must be derived from public proceeding facts; the user-specific role and deadline confirmation remain with the user backend. Stagger initial due dates and include forced-full and slow safety-check controls.

## Data ownership and completion details

- Public proceeding identity: application + proceeding type + proceeding number. Public notices require source/proceeding identity as well as `corres_no`; do not mix opposition-specific rows into ordinary application notices.
- Public compliance events record facts and source artifacts. Project tasks through each user's portfolio membership and party/side. Private receipt dates, deadline confirmations, notes and task statuses require user ownership and audit.
- Store fetch timestamp and portal “as on” date separately. The current result contract uses observation time for reservation validation/order; replacing it with a portal-only date would break that contract.
- Deadline rules need version, anchor, period value/unit, role applicability and approval. Month-based periods require calendar arithmetic, not a fixed number of days. Missing receipt/event dates remain unresolved; competitor displays are not legal ground truth.
- Field merging must preserve source provenance and distinguish missing/not-matched from an explicit blank or changed value. Historical values should not overwrite a newer explicit correction.

## Evidence still needed

No standalone normal panel responses/document list, real scheduled hearing notice, decision, rectification or live multiple-proceeding example were found in this snapshot set. Need those formats, actual PDFs with extraction gaps, and a limited live request trace for anti-forgery/locator behavior. No automatic rule periods or status intervals are inferred from this one application sample.
