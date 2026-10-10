-- Creates the accounts database and a dedicated, least-privilege user for the app.
-- Run as an administrator, e.g. with XAMPP:  C:\xampp\mysql\bin\mysql.exe -u root -p < scripts\mysql_setup.sql
-- Replace CHANGE_ME by a strong password, and put the same one in USERS_DB_URL (.env).
-- The app creates its tables (users, login_codes, login_events, questions) on first start.
CREATE DATABASE IF NOT EXISTS edan_chat CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE USER IF NOT EXISTS 'edan_app'@'localhost' IDENTIFIED BY 'CHANGE_ME';
CREATE USER IF NOT EXISTS 'edan_app'@'127.0.0.1' IDENTIFIED BY 'CHANGE_ME';
GRANT SELECT, INSERT, UPDATE, DELETE, CREATE, INDEX, REFERENCES ON edan_chat.* TO 'edan_app'@'localhost';
GRANT SELECT, INSERT, UPDATE, DELETE, CREATE, INDEX, REFERENCES ON edan_chat.* TO 'edan_app'@'127.0.0.1';
FLUSH PRIVILEGES;
