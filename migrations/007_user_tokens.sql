-- v0.7 — per-user GitHub/HF tokens for external artifact verification, so a
-- single shared server-wide token (or none at all) isn't the only option.
-- Encrypted (Fernet, see token_crypto.py) — this is the first reversible
-- secret this schema stores, unlike api_key's one-way hash.
ALTER TABLE app_user ADD COLUMN IF NOT EXISTS github_token_encrypted TEXT;
ALTER TABLE app_user ADD COLUMN IF NOT EXISTS huggingface_token_encrypted TEXT;
