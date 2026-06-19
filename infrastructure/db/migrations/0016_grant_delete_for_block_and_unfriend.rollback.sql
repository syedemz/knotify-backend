-- Rollback for migration 0016: revoke DELETE on friendships / friend_requests / blocks
REVOKE DELETE ON TABLE friendships     FROM app_user;
REVOKE DELETE ON TABLE friend_requests FROM app_user;
REVOKE DELETE ON TABLE blocks          FROM app_user;
