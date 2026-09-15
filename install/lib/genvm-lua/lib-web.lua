local M = {}

---@class WebRenderPayload
---@field url string
---@field mode "text" | "html" | "screenshot"
---@field wait_after_loaded number
---@field size_limit integer?

---@class WebRequestPayload
---@field url string
---@field method "GET" | "POST" | "HEAD" | "DELETE" | "OPTIONS" | "PATCH"
---@field headers table<string, string>
---@field body string?
---@field sign boolean?
---@field size_limit integer?

local lib = require("lib-genvm")

---@class WEB
---@field allowed_tld { [string]: boolean }
---@field config table
---@field get_webdriver_session fun(ctx): string

---@type WEB
M.rs = __web ---@diagnostic disable-line

--- Set of URL schemas permitted for web requests.
---@type table<string, boolean>
M.allowed_schemas = {
	["http"] = true,
	["https"] = true,
}

local function table_has_val(tab, val)
	for _, v in ipairs(tab) do
		if v == val then
			return true
		end
	end
	return false
end

--- Validate a URL against allowed schemas, ports, and TLDs.
--- Raises a non-fatal `user_error` if any check fails (MALFORMED_URL, SCHEMA_FORBIDDEN, PORT_FORBIDDEN, TLD_FORBIDDEN).
--- URLs whose host is in `config.always_allow_hosts` bypass port and TLD checks.
---@param url string the URL to validate
---@return boolean allowlisted whether the host is in `config.always_allow_hosts`
M.check_url = function(url)
	local split_url = lib.rs.split_url(url)

	if split_url == nil then
		lib.rs.user_error {
			causes = { "MALFORMED_URL" },
			fatal = false,
			ctx = {
				url = url,
			},
		}
	end
	---@cast split_url -nil

	if not M.allowed_schemas[split_url.schema] then
		lib.rs.user_error {
			causes = { "SCHEMA_FORBIDDEN" },
			fatal = false,
			ctx = {
				schema = split_url.schema,
				url = url,
			},
		}
	end

	lib.log {
		level = "debug",
		message = "checking url",
		schema = split_url.schema,
		always_allow_hosts = M.rs.config.always_allow_hosts,
		host = split_url.host,
	}

	if table_has_val(M.rs.config.always_allow_hosts, split_url.host) then
		return true
	end

	if split_url.port ~= nil and split_url.port ~= 80 and split_url.port ~= 443 then
		lib.rs.user_error {
			causes = { "PORT_FORBIDDEN" },
			fatal = false,
			ctx = {
				port = split_url.port,
				url = url,
			},
		}
	end

	local from = split_url.host:find("[.]([^.]*)$")
	if from == nil then
		from = 0 -- not 1 for +1
	end
	local tld = string.sub(split_url.host, from + 1)

	lib.log {
		level = "debug",
		message = "detected TLD",
		detected_tld = tld,
		host = split_url.host,
		from = from,
	}

	if not M.rs.allowed_tld[tld] then
		lib.rs.user_error {
			causes = { "TLD_FORBIDDEN" },
			fatal = false,
			ctx = {
				tld = tld,
				url = url,
			},
		}
	end

	return false
end

--- Re-raise a failure of *our own* webdriver sidecar.
---
--- Deliberately fatal. A broken sidecar means this validator has no valid
--- observation of the page at all, so it must not produce a contract-visible
--- result: doing that would let it assert a claim it never observed, and then
--- vote on it. Fatal becomes `ResultCode::InternalError`, which a node turns
--- into a Timeout vote -- the truthful answer, "I could not do the work".
--- A failure of the *remote site* is a real observation and stays non-fatal;
--- only this node's own infrastructure takes this path.
---
--- The cause is prepended rather than replacing what the transport reported,
--- so `STATUS_NOT_OK` / `SENDING_REQUEST` / `READING_BODY` survive as the
--- underlying detail. It is not contract-visible: a fatal `ModuleError` is
--- stringified into `FatalError` and never reaches the runner as `causes`, so
--- this word is for operators and for the node's logs.
---@param e any the caught error value
M.reraise_as_webdriver_unavailable = function(e)
	local err = lib.rs.as_user_error(e)
	if err == nil then
		-- Not a module error, so it carries no fatality flag; unclassified is
		-- already treated as fatal downstream. Re-raised untouched.
		error(e)
	end

	table.insert(err.causes, 1, "WEBDRIVER_UNAVAILABLE")
	err.fatal = true
	lib.rs.user_error(err)
end

return M
