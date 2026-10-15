"""The portal's trademark statuses and the recommended days between fetches for each.

The status screen in the control plane lets an administrator change any of these at any time;
these values are only the starting point ("Fill missing with recommended days"). This is a
monitoring cadence, not a legal deadline: nothing here infers or decides a filing date.
A value of None means the trademark is never fetched automatically in that status.
"""
import re

STATUS_DEFAULTS = (
    # Deadlines, hearings, oppositions and advertisement windows: weekly.
    ('Objected', 7), ('Exam Report Issued', 7), ('Set for Hearing', 7), ('Opposed', 7), ('Opposed*', 7),
    ('Accepted & Advertised', 7), ('Advertised', 7), ('Advertised bef acc', 7), ('ABA/Readvertised', 7),
    ('Accepted/Readvertise', 7), ('Send to Re-Advertised', 7), ('Formalities Chk Fail', 7),
    ('Rectification Filed', 7), ('Appeal', 7), ('Gone to court', 7), ('Stay of Registration', 7),
    ('Acceptance Withdrawn Process', 7), ('Review', 7),
    # Moving through the registry: every two weeks.
    ('New Application', 14), ('Online New Appl.', 14), ('Fresh After Payment', 14), ('Formalities Chk Pass', 14),
    ('Ready for Examination', 14), ('Marked for Exam', 14), ('Under Examination', 14), ('Sent Back to Examiner', 14),
    ('Send Back to EDP', 14), ('Send to PRAS', 14), ('Send To Vienna Codification', 14), ('Accepted', 14),
    ('Opposition Dismissed', 14), ('Opposition Withdrawn', 14), ('Provisional Refusal Confirmed', 14),
    ('Under Division', 14), ('Split', 14), ('Allowed', 14),
    # Refused: an appeal may still follow.
    ('Refused', 30), ('Protection Refused', 30),
    # Settled.
    ('Registered', 90), ('Protection Granted', 90), ('Disposed', 90),
    # Closed, but sometimes restored.
    ('Abandoned', 180), ('Deemed to be Abandoned', 180), ('Removed', 180),
    # Finished: never fetched automatically.
    ('Withdrawn', None), ('Cancelled', None), ('IR Cancelled', None), ('INVALID (No fee received)', None),
    ('Application Invalidated', None),
)


def policy_id(status):
    """Stable policy id for a status, e.g. 'Opposed*' -> 'st_opposed_star'."""
    text = status.lower().replace('*', ' star ').replace('&', ' and ')
    return 'st_' + re.sub(r'[^a-z0-9]+', '_', text).strip('_')[:45]
