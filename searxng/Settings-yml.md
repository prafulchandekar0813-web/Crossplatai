# SearXNG Configuration: Practical Implementation Guide

This guide explains how to apply, save, and verify variables inside your SearXNG `settings.yml` configuration file.

---

## 🛠️ Step 1: Locate and Open Your File

Depending on your installation type, open the configuration file using a terminal text editor (like `nano` or `vim`):

*   **Docker Compose Deployment (Most Common):** Mapped inside your project folder:
    ```bash
    nano ./searxng/settings.yml
    ```
*   **Bare-Metal / Linux Daemon Install:** Located in the global system configurations directory:
    ```bash
    sudo nano /etc/searxng/settings.yml
    ```

---

## 📝 Step 2: Apply the Variables (YAML Syntax Rules)

> ⚠️ **Critical Rule:** YAML syntax strictly requires **spaces for indentation**. Never use the `Tab` key. If the indentation is misaligned, SearXNG will crash on startup.

Copy and paste this production-ready configuration block into your file, adjusting values where necessary:

```yaml
# 1. Global Directive (Maintains master defaults)
use_default_settings: true

# 2. General Settings Block
general:
  instance_name: "My Private Search"
  debug: false                  # Always set to false in production for security
  enable_metrics: true          # Measures and logs slow downstream engines

# 3. Search Settings Block
search:
  safe_search: 1                # 0 = Off, 1 = Moderate, 2 = Strict
  autocomplete: "duckduckgo"     # Provides search suggestions as you type
  default_lang: "en"
  formats:                      # Allowed data output frameworks
    - html                      # For human browsing via web UI
    - json                      # REQUIRED for AI, LLM, or Open WebUI plugins

# 4. Server Configuration Block
server:
  port: 8888
  bind_address: "0.0.0.0"       # Allows Docker to route external network traffic
  secret_key: "replace_this_with_a_secure_random_string" 
  base_url: "http://localhost:8888" # Change to https://yourdomain.com in production
  limiter: false                # Set to false if connecting a heavy local AI script
  image_proxy: true             # Completely masks user IPs from image tracking networks

# 5. Outgoing Connections Block
outgoing:
  request_timeout: 2.5          # Abandons any single backend taking over 2.5 seconds

# 6. Downstream Engines Customization Block
engines:
  - name: google
    disabled: false
    weight: 1.5                 # Prioritizes Google results higher in aggregate lists
    
  - name: bing
    disabled: true              # Disables entirely to optimize bandwidth usage
```

---

## 🔄 Step 3: Apply the Changes (Restarting the App)

SearXNG reads its configurations *only* during boot. You must restart the runtime to apply changes.

*   **If you are using Docker:**
    ```bash
    docker compose restart searxng
    ```
*   **If you are using a native Linux systemd service:**
    ```bash
    sudo systemctl restart searxng
    ```

---

## 🧪 Step 4: Verify Your Configuration

Ensure your configurations took effect by performing these quick tests:

1.  **Check the UI Web Title:** Open your web browser and navigate to your instance URL. The top header title should reflect the exact value you input for `instance_name`.
2.  **Verify the Programmatic API (`json`):** Test if external applications can scrape your results by querying the engine endpoint directly in your browser:
    ```text
    http://localhost:8888/search?q=test&format=json
    ```
    If it yields a screen of raw data text instead of a webpage layout, your JSON engine is perfectly configured.
3.  **Troubleshoot Misconfigurations:** If the instance fails to load or shows errors, fetch the deployment logs to spot broken YAML syntax formatting:
    ```bash
    docker compose logs searxng
    # OR for native Linux installations:
    sudo journalctl -u searxng --no-pager -n 50
    ```
