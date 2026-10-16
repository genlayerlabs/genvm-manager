Web :term:`Module`
------------------

Web Scraping Capabilities
~~~~~~~~~~~~~~~~~~~~~~~~~

-  HTML parsing and data extraction
-  JavaScript execution and dynamic content handling via webdriver

HTTP Request Handling
~~~~~~~~~~~~~~~~~~~~~

-  **Protocol Support**:

   -  HTTP/HTTPS request execution
   -  Custom header specification and management
   -  Authentication mechanism support via RFC9421
   -  Proxy and routing configuration

-  **Error Handling**:

   -  Network timeout and retry mechanisms
   -  HTTP error code interpretation
   -  Connection failure recovery

Security and Compliance
~~~~~~~~~~~~~~~~~~~~~~~

-  Whitelist top-level domain filtering
-  Whitelist port filtering
-  URL pattern matching and validation
-  Execution time limits and timeouts

Webdriver Egress
~~~~~~~~~~~~~~~~

Each Chromium instance uses a local SOCKS5 proxy for HTTP, HTTPS and WebSocket
connections. The proxy rejects a destination if any resolved address fails
the SSRF address filter, then connects to a validated numeric IP without
another DNS lookup. TLS and the original hostname remain end-to-end

Chromium has no direct proxy fallback or implicit loopback bypass. Direct
DNS resolution, QUIC and non-proxied WebRTC UDP are disabled. A temporary
browser profile sets the WebRTC IP handling policy and is removed after the
browser exits. Extra arguments in ``GVM_WEBDRIVER_CHROME_ARGS`` that override
the routing controls or select another profile are rejected at startup

Request interception also checks URLs before navigation and subresource
loads; the proxy enforces the address check at connection time, including
when DNS answers change after interception
