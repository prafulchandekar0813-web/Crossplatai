# SearXNG Settings Configuration Guide

This reference document outlines the key variables available in the SearXNG `settings.yml` file, detailing their use cases and the operational benefits of configuring them.

---

## 🔝 Global Directives

### `use_default_settings`
* **Use Case:** Set to `true` when you want your custom `settings.yml` to act as a clean override layer. It instructs SearXNG to load its internal master defaults first, meaning you only need to declare variables you explicitly want to change.
* **Why to use:** It dramatically simplifies maintenance. Instead of managing a multi-thousand-line configuration file, your file stays tiny, readable, and highly stable during software updates.

---

## 🧱 1. General Settings (`general:`)

### `instance_name`
* **Use Case:** A string that names your search engine instance (e.g., `"PrivacySearch"`). It alters the visible header text on the web interface.
* **Why to use:** Personalises the instance for your users, team, or self-hosted dashboard.

### `debug`
* **Use Case:** A boolean (`true` or `false`) that controls whether the application prints full Python stack traces and developer logs to the console.
* **Why to use:** Indispensable during initial setup or troubleshooting to catch YAML formatting flaws. It must be turned off (`false`) in production environments to avoid leaking server internals.

### `enable_metrics`
* **Use Case:** Collects internal performance statistics, mapping how fast or slow external search engines respond to queries.
* **Why to use:** Helps you identify and disable lagging engines that are dragging down your global search performance.

---

## 🔍 2. Search Settings (`search:`)

### `safe_search`
* **Use Case:** Filters out explicit or adult content from search results using integers: `0` (Off), `1` (Moderate), or `2` (Strict).
* **Why to use:** Essential for tailoring the instance to your environment, whether creating a child-friendly portal or an unrestricted private instance.

### `autocomplete`
* **Use Case:** Chooses which provider generates instant search suggestions as you type (e.g., `"duckduckgo"`, `"google"`, `"wikipedia"`, or `""` for disabled).
* **Why to use:** Enhances the user experience by mimicking traditional search engines without sacrificing core privacy.

### `default_lang`
* **Use Case:** Configures the default fallback language and regional bias for queries (e.g., `"en"`, `"fr"`, or `"auto"` to match the browser header).
* **Why to use:** Ensures your search results are relevant to your primary language demographic out of the box.

### `formats`
* **Use Case:** Defines how search results can be delivered (e.g., `- html` for web browsers, `- json` for applications, or `- rss` for feeds).
* **Why to use:** **Crucial for AI and LLM integrations.** Adding `- json` enables third-party tools like Open WebUI, LangChain, or custom automation scripts to parse your search results programmatically.

---

## 🌐 3. Server Configuration (`server:`)

### `port`
* **Use Case:** Specifies the network port the internal application binds to (commonly `8888`).
* **Why to use:** Helps avoid port conflicts with other web applications running on the same host hardware.

### `bind_address`
* **Use Case:** Tells SearXNG which network interface to listen on. `"127.0.0.1"` locks down access to the local machine, while `"0.0.0.0"` opens it to outside traffic or Docker bridges.
* **Why to use:** Secures the environment. Using `"127.0.0.1"` ensures that internet traffic cannot access your engine directly, forcing external connections through a secure reverse proxy (like Nginx or Caddy).

### `secret_key`
* **Use Case:** A unique, random cryptographic string used to sign session cookies and prevent cross-site request forgery (CSRF) attacks.
* **Why to use:** A vital security requirement. Failing to replace the default key compromises the security of user sessions on your instance.

### `base_url`
* **Use Case:** The absolute URL where the instance is hosted (e.g., `https://mydomain.com`).
* **Why to use:** Ensures all static assets, media links, and pagination buttons resolve accurately without breaking when exposed behind a reverse proxy or SSL certificate.

### `limiter`
* **Use Case:** Toggles a built-in security firewall (`true` / `false`) that tracks client request volumes to prevent scraping attacks.
* **Why to use:** Keeps your server stable. Leave it enabled (`true`) for public sites to stop abusive bots. Turn it off (`false`) if the instance is restricted to a private AI framework that fires intensive bursts of queries.

### `image_proxy`
* **Use Case:** Routes all image results through your SearXNG server instead of letting the user's browser fetch them directly from source websites.
* **Why to use:** Guarantees user privacy. It stops third-party tracking scripts from discovering your end-users' IP addresses when they browse the image results tab.

---

## 📡 4. Outgoing Request Configuration (`outgoing:`)

### `request_timeout`
* **Use Case:** Specifies the maximum time in seconds (e.g., `3.0`) SearXNG will wait for an upstream engine to respond before abandoning it.
* **Why to use:** Optimises overall search speed. A single stalling engine will not delay the delivery of results from faster engines.

### `proxies`
* **Use Case:** Routes outgoing scrapers through external proxy networks or Tor layers.
* **Why to use:** Prevents your server's primary IP from being blacklisted or challenged with CAPTCHAs by strict engines like Google or Bing.

---

## 🛠️ 5. Engine Customization (`engines:`)

### `disabled`
* **Use Case:** Disables specific search backends globally (`true` or `false`).
* **Why to use:** Streamlines execution by cutting out platforms you do not use (e.g., disabling individual torrent or social media scrapers to reduce network load).

### `weight`
* **Use Case:** A numerical multiplier (e.g., `2.0`) used to bias the relevancy ranking algorithm.
* **Why to use:** Allows you to prioritize high-quality platforms. If you trust Wikipedia data over Reddit, increasing Wikipedia's weight pushes its links to the top of aggregate results.

### `tokens`
* **Use Case:** Restricts specific engines behind an authorization token key.
* **Why to use:** Useful if you want to integrate a paid API or custom private crawler, ensuring only authorized users can trigger requests to that specific backend.
