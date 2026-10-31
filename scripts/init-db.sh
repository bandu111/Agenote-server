#!/bin/sh
set -eu
# 独立、非超级用户的应用角色；密码只经环境传递，不输出。
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname postgres --set=app_password="$APP_DB_PASSWORD" <<'SQL'
CREATE ROLE yanshu_app LOGIN PASSWORD :'app_password';
CREATE DATABASE yanshu OWNER yanshu_app;
CREATE DATABASE yanshu_auth_test OWNER yanshu_app;
SQL
