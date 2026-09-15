ALTER TABLE jobs ADD COLUMN source TEXT NOT NULL DEFAULT 'web';
ALTER TABLE jobs ADD COLUMN discord_user_id TEXT;
ALTER TABLE jobs ADD COLUMN discord_channel_id TEXT;
ALTER TABLE jobs ADD COLUMN discord_last_status TEXT;
