---
name: data-storage
description: Make an app save its data where the owner chose (on our server or their own online database) by reading DATABASE_URL and DATA_DIR, and write the choice to data.json.
---
The platform tells every hosted app where its data lives through environment variables. Never hard-code a database address, a password or a file path.
- DATABASE_ENGINE: sqlite (on our server) | postgres | mysql | mongodb | redis | couchdb | libsql | firestore (only in the owner's own online database).
- DATABASE_URL: the address. On our server it is always sqlite:////data/app.db: one .db file, nothing else is offered locally, so write the app to use SQLite there. For other engines it is the address of the owner's own online provider. Redis uses REDIS_URL. Firestore uses FIREBASE_SERVICE_ACCOUNT (the key file's JSON text).
- DATA_DIR: a folder that survives restarts (uploads, files). Use it for anything that must not be lost.
Write data.json at the project root with the owner's answer: {"mode": "local"} for the .db file on our server, or {"mode": "online", "engine": "postgres", "provider": "supabase"}. The owner pastes the address in Manage app (never in chat) and can change it there; data.json is only the starting choice. Never ask the user to type a password or address into the chat.

Adapting the project:
1. Read the project's data layer first (PROJECT.md lists the database it uses and the ORM).
2. It uses an ORM (SQLAlchemy, Django ORM, Prisma, Sequelize, Peewee, Mongoose): read the connection from DATABASE_URL and keep the models. Change only the engine setting and add the driver to requirements.txt or package.json. Create tables on start (create_all / migrate), because the database starts empty.
3. It uses hand-written SQL for one engine: change the queries only as far as the chosen engine needs (for the .db file, SQLite), and test them.
4. The owner chose a different family from the project (a SQL app on MongoDB): that is a rewrite of the data layer. Say so in one line and offer to keep the project's own database type in the owner's own online database, or move it to SQLite on our server. Only rewrite if the owner insists.
5. Pin the driver versions. Use a connection pool with a small size (5) and retry the first connection for up to 30 seconds, because the database may still be starting.
6. Online databases need the provider to allow connections from this server's IP address: tell the owner to check that in the provider's network settings.
Existing data is not copied when the owner switches the place; tell them to expect an empty app there.
