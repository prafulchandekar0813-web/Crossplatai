# SearXNG settings.yml Configuration Manual

This document details the configuration variables for SearXNG's `settings.yml` file, covering their use cases, why you should configure them, and exactly how to use them.

---

## 🔝 Global Directives

### `use_default_settings`
*   **Use Case:** Tells SearXNG to load its internal master template first. Your custom configuration will only override or append to those defaults.
*   **Why to use:** It dramatically simplifies updates. You do not need to maintain a massive multi-thousand-line file. Your config stays tiny and clean.
*   **How to use:**
    ```yaml
    use_default_settings: true
    ```

---

## 🧱 1. General Settings (`general:`)

### `instance_name`
*   **Use Case:** A text string that labels your search engine instance.
*   **Why to use:** It customizes the visible title on the top header of the web page interface.
*   **How to use:**
    ```yaml
    general:
      instance_name: "My Private Search Engine"
    ```

### `debug`
*   **Use Case:** A toggle (`true` or `false`) that activates verbose application trace logs.
*   **Why to use:** Turn it on during initial setups to catch YAML errors or broken backend connectors. Turn it off (`false`) in production to secure server internals from being leaked.
*   **How to use:**
    ```yaml
    general:
      debug: false
    ```

### `enable_metrics`
*   **Use Case:** Monitors internal performance by tracking individual engine response speeds.
*   **Why to use:** Provides a diagnostics page to help you spot and disable slow downstream engines that are bottlenecking your experience.
*   **How to use:**
    ```yaml
    general:
      enable_metrics: true
    ```

---

## 🔍 2. Search Settings (`search:`)

### `safe_search`
*   **Use Case:** Filters explicit content out of results.
*   **Why to use:** Adjusts content visibility depending on who uses the engine (`0` = Disabled, `1` = Moderate filtering, `2` = Strict filtering).
*   **How to use:**
    ```yaml
    search:
      safe_search: 1
    ```

### `autocomplete`
*   **Use Case:** Chooses the third-party backend used to generate suggestions as you type.
*   **Why to use:** Mimics a modern search engine experience without letting the provider track you directly.
*   **How to use:** Set to engines like `"duckduckgo"`, `"google"`, or `""` to completely disable it.
    ```yaml
    search:
      autocomplete: "duckduckgo"
    ```

### `default_lang`
*   **Use Case:** Sets the default fallback regional language code.
*   **Why to use:** Ensures results lean toward your preferred language right out of the box when the browser does not specify one.
*   **How to use:** Set to an ISO language code or `"auto"` to match the user's browser language.
    ```yaml
    search:
      default_lang: "auto"
    ```

### `formats`
*   **Use Case:** Defines what types of data formats the search endpoint will output.
*   **Why to use:** **Crucial for AI / Open WebUI applications.** Adding `- json` enables external LLMs and scripts to parse your search engine results programmatically.
*   **How to use:**
    ```yaml
    search:
      formats:
        - html
        - json
    ```

---

## 🌐 3. Server Configuration (`server:`)

### `port`
*   **Use Case:** The network port the SearXNG web app listens on.
*   **Why to use:** Prevents networking conflicts if you host other containers or apps on the same hardware.
*   **How to use:**
    ```yaml
    server:
      port: 8888
    ```

### `bind_address`
*   **Use Case:** Binds the application to a specific local or public network adapter interface.
*   **Why to use:** `"127.0.0.1"` locks the service down to the host machine for safety behind a reverse proxy. `"0.0.0.0"` allows Docker engines to route outside traffic into the container.
*   **How to use:**
    ```yaml
    server:
      bind_address: "0.0.0.0"
    ```

### `secret_key`
*   **Use Case:** A unique, random cryptographic string used to sign session cookies and mitigate cross-site scripting (CSRF) vulnerabilities.
*   **Why to use:** Critical security requirement. Leaving the default value compromises session security.
*   **How to use:** (Generate a hex string using a terminal tool like `openssl rand -hex 32`)
    ```yaml
    server:
      secret_key: "your_generated_secure_hex_string_here"
    ```

### `base_url`
*   **Use Case:** The absolute external domain mapping of your application.
*   **Why to use:** Fixes routing layout issues for page assets, images, and page navigation when accessing SearXNG behind reverse proxies or SSL certificates.
*   **How to use:**
    ```yaml
    server:
      base_url: "https://mydomain.com"
    ```

### `limiter`
*   **Use Case:** A built-in traffic firewall that blocks scraper bots and excessive rapid requests.
*   **Why to use:** Keeps a public web page instance from crashing due to scraping abuse. **Note:** Turn this off (`false`) if your engine acts exclusively as a local backend for an intensive AI pipeline to prevent getting blocked by your own firewall.
*   **How to use:**
    ```yaml
    server:
      limiter: false
    ```

### `image_proxy`
*   **Use Case:** Routes all image search results directly through your server instead of the browser.
*   **Why to use:** Strips external trackers from discovering your users' IP addresses when viewing images.
*   **How to use:**
    ```yaml
    server:
      image_proxy: true
    ```

---

## 📡 4. Outgoing Request Configuration (`outgoing:`)

### `request_timeout`
*   **Use Case:** Max time in seconds SearXNG waits for a downstream engine to answer.
*   **Why to use:** Optimizes response latency. A slow or hanging website won't slow down the response time of the rest of the search query.
*   **How to use:**
    ```yaml
    outgoing:
      request_timeout: 2.5
    ```

### `proxies`
*   **Use Case:** Routes outbound scrapers through an external proxy list or the Tor network.
*   **Why to use:** Prevents your primary home or server IP from being challenged with CAPTCHAs or banned by target engines like Google or Bing.
*   **How to use:**
    ```yaml
    outgoing:
      proxies:
        http:
          - http://example.com
        https:
          - http://example.com
    ```

---

## 🛠️ 5. Engine Customization (`engines:`)

### `disabled`
*   **Use Case:** Removes a built-in search engine backend provider from global search operations.
*   **Why to use:** Streamlines results and cuts down outgoing traffic on targets you do not care about.
*   **How to use:**
    ```yaml
    engines:
      - name: bing
        disabled: true
