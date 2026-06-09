# Security

MedNotes is for public medical education material, not private patient data.

## Reporting

If you find a security issue, open a private advisory on GitHub if available or
contact the maintainer directly. Do not disclose private data in public issues.

## Data boundary

Do not commit, attach, or paste:

- patient names
- patient identifiers
- private clinical notes
- API keys or credentials
- `.env` files

The local public guard catches obvious private paths and secret markers, but it
is not a substitute for human review.
