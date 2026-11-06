# Your own SearXNG (search server for Agent Workbench)

Public instances such as searx.be answer scripts with a "Verifying your browser" page, so they cannot be used.
Run your own; it takes a minute.

**On your own machine / server**
    docker build -t my-searxng searxng
    docker run -d --name searxng -p 8888:8080 -e SEARXNG_SECRET=$(openssl rand -hex 32) my-searxng
    # then:  SEARCH_PROVIDER=searxng   SEARCH_BASE_URL=http://localhost:8888
    # check: curl "http://localhost:8888/search?q=test&format=json"   (must print JSON)

**On Render**
1. Put this `searxng/` folder in a GitHub repo (its own repo, or a subfolder).
2. New → Web Service → that repo, Runtime = Docker (root directory `searxng` if it's a subfolder).
3. Environment: `SEARXNG_SECRET` = any long random string. Port 8080.
4. Copy the service URL (e.g. https://my-searxng.onrender.com) into the agent's `SEARCH_BASE_URL`.

The instance has no login, so anyone who knows the URL can search through it. Keep the URL private
(on Render, a Private Service in the same account avoids public exposure).
