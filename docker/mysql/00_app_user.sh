#!/bin/bash
# Runs ONCE, on the first start of the MySQL container (docker-entrypoint-initdb.d),
# before sql/01_schema.sql. Creates the non-root application user that the Python
# code logs in with. Credentials come from the container environment (docker-compose.yml).
set -e
mysql -uroot -p"${MYSQL_ROOT_PASSWORD}" <<SQL
CREATE USER IF NOT EXISTS '${APP_DB_USER}'@'%' IDENTIFIED BY '${APP_DB_PASSWORD}';
GRANT ALL PRIVILEGES ON university_timetable.* TO '${APP_DB_USER}'@'%';
FLUSH PRIVILEGES;
SQL
