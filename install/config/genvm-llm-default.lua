local lib = require("lib-genvm")
local llm = require("lib-llm")
local llm_policy = require("llm_policy")
local sqlite3 = require("lsqlite3")

-- There is no guarantee that different genvm executions will be executed in the same lua VM.
-- Moreover, multiple genvms can be executed in parallel, so avoid using global state.
-- Instead, each genvm creates a session, which has a single `ctx` object,
-- which is preserved across multiple calls

--- GEN-wei charged per token, before rounding the total charge.
---@param ctx any execution context
---@param provider string provider id
---@param model string model name
---@return Rat GEN-wei charged per token
local function gen_per_token(ctx, provider, model)
	local meta = llm.providers[provider].models[model].meta
	local rate = meta and meta.time_units_per_1k_tokens
	if rate == nil then
		rate = "1/4"
	end
	assert(type(rate) == "string", "time_units_per_1k_tokens must be a rational string")
	rate = rat.new(rate)
	assert(rate >= rat.zero, "time_units_per_1k_tokens must be non-negative")
	return (ctx.gen_per_time_unit or rat.zero) * rate / rat.new(1000)
end

local function get_or_create_stats(ctx, provider, model)
	local key = provider .. "/" .. model
	local entry = ctx.stats[key]
	if entry == nil then
		entry = {
			error_count = 0,
			input_tokens = 0,
			output_tokens = 0,
			total_tokens = 0,
			cache_read_tokens = 0,
			cache_write_tokens = 0,
			image_units = 0,
		}
		ctx.stats[key] = entry
	end
	return entry
end

---@param request { prompt: Prompt, format: Format, model: string, provider: string, timeout: number | nil }
---@param calc_consumed_gen fun(res: ProviderResult): Rat | number | nil
---@return ProviderResult
local function exec_update_policy_data(ctx, request, calc_consumed_gen)
	local success, res = pcall(llm.rs.exec_prompt_in_provider, ctx, request)

	local entry = get_or_create_stats(ctx, request.provider, request.model)

	if success then
		local consumed_gen = calc_consumed_gen(res)
		if consumed_gen then
			ctx.policy.spent_gen_wei = ctx.policy.spent_gen_wei + rat.new(consumed_gen)
		end
		if ctx.policy.stop_on_spent and ctx.policy.spent_gen_wei >= ctx.policy.stop_on_spent then
			ctx.policy.exhausted = true
		end

		local t = res.tokens
		if t then
			entry.input_tokens = entry.input_tokens + (t.input or 0)
			entry.output_tokens = entry.output_tokens + (t.output or 0)
			entry.total_tokens = entry.total_tokens + (t.total or 0)
			entry.cache_read_tokens = entry.cache_read_tokens + (t.cache_read or 0)
			entry.cache_write_tokens = entry.cache_write_tokens + (t.cache_write or 0)
			entry.image_units = entry.image_units + (t.image_units or 0)
		end
		return res
	else
		entry.error_count = entry.error_count + 1

		error(res)
	end
end

local MAX_BUDGET_TIMEOUT = rat.new("60")

local function compute_timeout(ctx, remaining_gen)
	if ctx.gen_per_time_unit == nil then
		return nil
	end
	local timeout = remaining_gen / ctx.gen_per_time_unit * rat.new("3/2")
	if timeout <= rat.zero or timeout > MAX_BUDGET_TIMEOUT then
		return nil
	end
	return timeout:to_float()
end

-- llm_policy owns backend selection and the failure state machine; this script
-- keeps transport, gen accounting, stats, timeouts and the error contract.
--
-- The catalog keys a model by "<provider>/<model>", not the bare model name: two
-- backends may serve the same name with different capabilities, and one key
-- would let one backend's capabilities vouch for another's. The chain reproduces
-- the pre-engine ordering: priority desc, provider name desc, model name asc.
--
-- Once any model declares `meta.greybox = { text = N, image = M }`, prompts walk
-- the chain for their modality instead and models outside it are never tried.
-- Lower N goes first; ties are ordered by sha256("<node address>/<catalog key>"),
-- fixed for one node and different across nodes.
local PROFILE_NAME = "compat"
local MAX_CHAIN_ENTRIES = require("llm_policy.term").LIMITS.max_param_entries
local GREYBOX_PROFILES = { text = "greybox_text", image = "greybox_image" }

local function sorted_greybox_chain(entries, node_address)
	-- hex, since `<` on strings goes through the locale-dependent strcoll
	local tie_key = {}
	for _, e in ipairs(entries) do
		local digest = lib.rs.sha2_256(node_address .. "/" .. e.model)
		tie_key[e.model] = digest:gsub(".", function(c)
			return string.format("%02x", c:byte())
		end)
	end
	local sorted = table.move(entries, 1, #entries, 1, {})
	table.sort(sorted, function(a, b)
		if a.priority ~= b.priority then
			return a.priority < b.priority
		end
		return tie_key[a.model] < tie_key[b.model]
	end)
	local chain = {}
	for _, e in ipairs(sorted) do
		table.insert(chain, { provider = e.provider, model = e.model })
	end
	return chain
end

-- Image filters a flat recipe record can express; Denoise is unimplemented and
-- Unsharpen takes two numbers.
local MUTATE_IMAGE_FILTERS = { GuassianNoise = true, JPEG = true }
-- Only temperature reaches the provider call; other params would be dropped.
local MUTATE_PARAM_OPS = { jitter = true, set_param = true, clamp = true }

local function check_mutate(spec)
	if spec == nil or spec == "identity" then
		return
	end
	if type(spec) ~= "table" then
		error("meta.mutate: invalid step " .. tostring(spec))
	end
	-- the engine compiles only the first operation it finds in a record
	local key_count, named = 0, false
	for key, _ in pairs(spec) do
		key_count = key_count + 1
		named = named or type(key) ~= "number"
	end
	if named and key_count > 1 then
		error("meta.mutate: a step holds 1 operation; compose several with pipe")
	end
	for key, value in pairs(spec) do
		if type(key) == "number" then
			check_mutate(value)
		elseif key == "pipe" then
			for _, step in ipairs(value) do
				check_mutate(step)
			end
		elseif key == "filter_text" then
			local ok, err = pcall(lib.rs.filter_text, "", value)
			if not ok then
				error("meta.mutate: invalid filter_text recipe: " .. tostring(err))
			end
		elseif key == "filter_image" then
			for _, filter in ipairs(value) do
				local name = type(filter) == "table" and next(filter) or nil
				local arg = name ~= nil and filter[name] or nil
				if not MUTATE_IMAGE_FILTERS[name] or type(arg) ~= "number" or next(filter, name) ~= nil then
					error("meta.mutate: filter_image supports { GuassianNoise = n } and { JPEG = q } only")
				end
			end
		elseif MUTATE_PARAM_OPS[key] then
			for param, arg in pairs(value) do
				if param ~= "temperature" or type(arg) ~= "number" then
					error("meta.mutate: " .. key .. " supports a numeric temperature only")
				end
			end
		else
			error("meta.mutate: unknown step " .. tostring(key))
		end
	end
end

local function build_policy_config()
	local providers = {}
	local models = {}
	local chain = {}
	local greybox = { text = {}, image = {} }
	local has_greybox = false

	local provider_names = {}
	for provider_name, _ in pairs(llm.providers) do
		table.insert(provider_names, provider_name)
	end

	table.sort(provider_names, function(a, b)
		local a_meta = llm.providers[a].meta
		local b_meta = llm.providers[b].meta
		local a_priority = a_meta and a_meta.priority or 0
		local b_priority = b_meta and b_meta.priority or 0

		if a_priority ~= b_priority then
			return a_priority > b_priority
		end
		return a > b -- just compare names
	end)

	for _, provider_name in ipairs(provider_names) do
		local provider_data = llm.providers[provider_name]

		-- Placeholders: the engine validates these but never dereferences them;
		-- host and key stay in Rust.
		providers[provider_name] = {
			base_url = "managed-by-genvm",
			api_kind = "openai_compatible",
			auth_env = "managed-by-genvm",
			tier = "partner",
			discovery = "static",
		}

		local model_names = {}
		for model_name, _ in pairs(provider_data.models or {}) do
			table.insert(model_names, model_name)
		end
		table.sort(model_names)

		for _, model_name in ipairs(model_names) do
			local model_data = provider_data.models[model_name]
			local family = provider_name .. "/" .. model_name

			models[family] = {
				served_by = {
					{ provider = provider_name, provider_model_id = model_name },
				},
				capabilities = {
					supports_json_mode = model_data.supports_json or false,
					supports_vision = model_data.supports_image or false,
					supports_seed = true,
				},
			}

			table.insert(chain, { provider = provider_name, model = family })

			local greybox_meta = nil
			if type(model_data.meta) == "table" then
				greybox_meta = model_data.meta.greybox
			end
			if greybox_meta ~= nil then
				if type(greybox_meta) ~= "table" or next(greybox_meta) == nil then
					error(family .. ": meta.greybox must be a table setting text and/or image")
				end
				for key, _ in pairs(greybox_meta) do
					if greybox[key] == nil then
						error(family .. ": unknown meta.greybox key " .. tostring(key))
					end
				end
				has_greybox = true
				for modality, entries in pairs(greybox) do
					local priority = greybox_meta[modality]
					if priority ~= nil then
						if type(priority) ~= "number" then
							error(family .. ": meta.greybox." .. modality .. " must be a number")
						end
						table.insert(entries, { provider = provider_name, model = family, priority = priority })
					end
				end
			end
		end
	end

	-- greybox chains are subsets of this one
	if #chain > MAX_CHAIN_ENTRIES then
		error(string.format("the catalog has %d models; the engine routes at most %d", #chain, MAX_CHAIN_ENTRIES))
	end

	-- `meta.mutate` is the engine's declarative mutate spec, applied per attempt;
	-- absent means the request goes out as the prompt built it.
	local mutate = nil
	if type(llm.rs.meta) == "table" then
		mutate = llm.rs.meta.mutate
	end
	check_mutate(mutate)

	local profiles = {
		[PROFILE_NAME] = {
			selector = "chain",
			chain = chain,
			retry_policy = PROFILE_NAME,
			mutate = mutate,
		},
	}
	if has_greybox then
		-- the per-node chain comes with each contract; this one only has to compile
		for modality, entries in pairs(greybox) do
			profiles[GREYBOX_PROFILES[modality]] = {
				selector = "chain",
				chain = sorted_greybox_chain(entries, ""),
				retry_policy = PROFILE_NAME,
				mutate = mutate,
			}
		end
	else
		greybox = nil
	end

	return {
		providers = providers,
		models = models,
		profiles = profiles,
		-- Every failure kind falls through to the next candidate, preserving the
		-- pre-engine behaviour of retrying on any user error, overloaded or not.
		retry_policies = {
			[PROFILE_NAME] = {
				unknown = { action = "next_candidate" },
			},
		},
	},
		greybox
end

-- The engine reads `host` as a plain global. Omitting `call_provider`/`sleep_ms`
-- keeps its blocking driver unreachable: this script drives execute_step itself.
_G.host = {
	now_ms = function()
		return lib.rs.monotonic_ms()
	end,
	log = function(level, event, fields, audience)
		lib.log { level = level, message = event, fields = fields, audience = audience }
	end,
}

-- GREYBOX_ENTRIES: { text = {...}, image = {...} } unsorted, nil without meta.greybox
local POLICY_CONFIG, GREYBOX_ENTRIES = build_policy_config()

do
	-- Reject a bad catalog at module start, not on the first prompt.
	local ok, err = llm_policy.init(POLICY_CONFIG)
	if not ok then
		error("llm_policy rejected the generated catalog: " .. tostring(err))
	end
	-- profiles compile lazily; ranking each one surfaces a bad `meta.mutate` now
	for name, _ in pairs(POLICY_CONFIG.profiles) do
		local ok, ranked, rank_err = pcall(llm_policy.rank, { profile = name })
		if not ok or ranked == nil then
			error("llm_policy rejected profile " .. name .. ": " .. tostring(ok and rank_err or ranked))
		end
	end
end

--- Describe a mapped prompt to the engine: which capabilities a candidate must
--- have to be eligible. Mirrors the checks `llm.select_providers_for` performs.
---@param mapped_prompt MappedPrompt
---@param seed integer
---@return table
local function build_contract(ctx, mapped_prompt, seed)
	local needs = {}
	local modality = "text"
	if lib.get_first_from_table(mapped_prompt.prompt.images) ~= nil then
		table.insert(needs, "vision")
		modality = "image"
	end

	local profile = PROFILE_NAME
	local chain = nil
	if ctx.greybox_chains ~= nil then
		profile = GREYBOX_PROFILES[modality]
		chain = ctx.greybox_chains[modality]
	end

	local response_format = nil
	if mapped_prompt.format == "json" or mapped_prompt.format == "bool" then
		-- bool is parsed out of a json object, so it needs json capability too
		response_format = { type = "json_object" }
	end

	return {
		profile = profile,
		chain = chain,
		requirements = { needs = needs },
		response_format = response_format,
		seed = seed,
		temperature = mapped_prompt.prompt.temperature,
	}
end

-- Cap on how long a policy-requested backoff may block one prompt.
local MAX_WAIT_SECONDS = 30

-- Run the (provider, model) the engine asked for. Returns the answer on success
-- (nil otherwise) and the feedback table to hand back to `execute_step`. A
-- non-user error is re-raised: it is a runtime fault, not a routing signal.
local function run_candidate(ctx, mapped_prompt, timeout, request)
	local provider_name = request.provider_id
	local model_name = request.served_model_id
	local model_data = llm.providers[provider_name].models[model_name]

	local prompt = {}
	for k, v in pairs(mapped_prompt.prompt) do
		prompt[k] = v
	end
	prompt.use_max_completion_tokens = model_data.use_max_completion_tokens
	prompt.seed = request.seed
	prompt.temperature = math.max(0, request.temperature or prompt.temperature)

	-- filter directives attached by `meta.mutate`; the engine only records them
	local filters = request._filters or {}
	if filters.text ~= nil then
		prompt.user_message = lib.rs.filter_text(prompt.user_message, filters.text.recipe)
		if prompt.system_message ~= nil then
			prompt.system_message = lib.rs.filter_text(prompt.system_message, filters.text.recipe)
		end
	end
	if filters.image ~= nil then
		local images = {}
		for i, img in ipairs(prompt.images) do
			local ok, filtered = pcall(lib.rs.filter_image, img, filters.image.recipe)
			if not ok then
				lib.log { level = "warning", message = "image filter failed", index = i, error = tostring(filtered) }
				lib.rs.user_error {
					causes = { "INVALID_IMAGE" },
					fatal = false,
					ctx = { index = i },
				}
			end
			images[i] = filtered
		end
		prompt.images = images
	end

	local call = {
		provider = provider_name,
		model = model_name,
		prompt = prompt,
		format = mapped_prompt.format,
		timeout = timeout,
	}

	lib.log { level = "trace", message = "calling exec_prompt_in_provider", request = call }
	local token_price = gen_per_token(ctx, call.provider, call.model)
	local success, result = pcall(exec_update_policy_data, ctx, call, function(res)
		-- convert token usage into gen using the per-model rate, and report it
		-- back as the gen consumed by this call (charged to the host as fuel)
		local total_tokens = (res.tokens and res.tokens.total) or 0
		local consumed_gen = (rat.new(total_tokens) * token_price):ceil()
		res.consumed_gen = consumed_gen
		return consumed_gen
	end)

	if success then
		return result, { ok = true }
	end

	local as_user_error = lib.rs.as_user_error(result)
	if as_user_error == nil then
		lib.log { level = "warning", audience = "operator", message = "non-user-error", original = result }
		error(result)
	end

	local status = as_user_error.ctx and as_user_error.ctx.status
	lib.log {
		level = "warning",
		audience = "operator",
		message = "provider failed, looking for next",
		error = as_user_error,
	}

	return nil,
		{
			ok = false,
			error_kind = llm.provider_http_error_description(status),
			http_status = status,
			-- the engine stores this as a plain string in its trace
			error_message = table.concat(as_user_error.causes or {}, ","),
		}
end

-- Sleep out a policy-requested backoff, bounded by MAX_WAIT_SECONDS. `until_ms`
-- is on the same monotonic clock the engine reads through `host.now_ms`.
local function wait_until(until_ms)
	local delay = ((until_ms or 0) - lib.rs.monotonic_ms()) / 1000
	if delay <= 0 then
		return
	end
	lib.log { level = "debug", audience = "operator", message = "policy backoff", seconds = delay }
	lib.rs.sleep_seconds(math.min(delay, MAX_WAIT_SECONDS))
end

local function dispatch_prompt(ctx, mapped_prompt, remaining_gen)
	---@cast mapped_prompt MappedPrompt

	local timeout = compute_timeout(ctx, remaining_gen)
	if timeout and timeout < 1 then
		lib.log {
			level = "warning",
			audience = "user",
			message = "computed timeout is very low, failing immediately",
			timeout = timeout,
		}
		llm.exhaust()
	end

	-- Per-call sampling seed: true randomness so validators diverge, logged so a
	-- call can be matched to its record.
	local b1, b2, b3, b4 = string.byte(lib.rs.random_bytes(4), 1, 4)
	local seed = (((b1 * 256 + b2) * 256 + b3) * 256 + b4) % 2147483647
	local host_data = ctx.host_data or {}
	lib.log {
		level = "info",
		message = "llm call seed",
		seed = seed,
		tx_id = host_data.tx_id,
		node_address = host_data.node_address,
	}

	local contract = build_contract(ctx, mapped_prompt, seed)

	lib.log {
		level = "debug",
		message = "executing prompt in backend",
		prompt = mapped_prompt,
		contract = contract,
	}

	local step = llm_policy.execute_step(nil, contract)
	local answer = nil

	while step.status ~= "done" do
		if step.status == "call" then
			local this_answer, feedback = run_candidate(ctx, mapped_prompt, timeout, step.request)
			answer = this_answer or answer
			step = llm_policy.execute_step(step.state_handle, nil, feedback)
		elseif step.status == "wait" then
			wait_until(step.until_ms)
			step = llm_policy.execute_step(step.state_handle, nil, nil)
		else
			error("unexpected policy step status: " .. tostring(step.status))
		end
	end

	if step.result.ok then
		return answer
	end

	lib.log {
		level = "error",
		audience = "operator",
		message = "no provider could handle prompt",
		error = step.result.error,
		trace = step.result.trace,
	}
	lib.rs.user_error {
		causes = { "NO_PROVIDER_FOR_PROMPT" },
		fatal = true,
		ctx = {
			prompt = mapped_prompt.prompt,
			trace = step.result.trace,
		},
	}
end

function Setup(ctx)
	ctx.stats = {}

	-- The engine keeps mutable routing state (breakers, latency EMA) in module
	-- scope and a VM is reused across sessions; reset per session so a decision
	-- never depends on which pooled VM this session got.
	local ok, err = llm_policy.init(POLICY_CONFIG)
	if not ok then
		error("llm_policy rejected the generated catalog: " .. tostring(err))
	end

	if GREYBOX_ENTRIES ~= nil then
		local node_address = (ctx.host_data or {}).node_address or ""
		ctx.greybox_chains = {}
		for modality, entries in pairs(GREYBOX_ENTRIES) do
			ctx.greybox_chains[modality] = sorted_greybox_chain(entries, node_address)
		end
	end

	local gen_per_time_unit_str = ctx.gas_data and ctx.gas_data.genPerTimeUnit
	local gen_per_time_unit = nil
	if gen_per_time_unit_str then
		local r = rat.new(gen_per_time_unit_str)
		if not r:is_zero() then
			gen_per_time_unit = r
		end
	end
	ctx.gen_per_time_unit = gen_per_time_unit

	local stop_on_spent = nil
	if gen_per_time_unit then
		stop_on_spent = gen_per_time_unit * rat.new(ctx.initial_time_units_allocation)
	end

	ctx.policy = {
		spent_gen_wei = rat.zero,
		exhausted = false,
		stop_on_spent = stop_on_spent,
	}
end

function Teardown(ctx)
	local data_dir = lib.rs.data_dir

	local has_stats = false
	for _ in pairs(ctx.stats) do
		has_stats = true
		break
	end
	if not has_stats then
		return
	end

	-- Stats persistence is best-effort telemetry: swallow failures so a transient
	-- SQLite issue (locked db, disk full, malformed file) does not break session
	-- teardown. `lib.finally` guarantees the db/stmt are released even on error.
	local ok, err = pcall(function()
		local db_path = data_dir .. "/stats.sqlite"
		local db = sqlite3.open(db_path)
		if not db then
			error("failed to open stats db at " .. db_path)
		end

		lib.finally(function()
			db:exec([[
				CREATE TABLE IF NOT EXISTS provider_stats (
					provider_model TEXT PRIMARY KEY,
					error_count INTEGER NOT NULL DEFAULT 0,
					input_tokens INTEGER NOT NULL DEFAULT 0,
					output_tokens INTEGER NOT NULL DEFAULT 0,
					total_tokens INTEGER NOT NULL DEFAULT 0,
					cache_read_tokens INTEGER NOT NULL DEFAULT 0,
					cache_write_tokens INTEGER NOT NULL DEFAULT 0,
					image_units INTEGER NOT NULL DEFAULT 0
				)
			]])

			local stmt = db:prepare([[
				INSERT INTO provider_stats (
					provider_model, error_count,
					input_tokens, output_tokens, total_tokens,
					cache_read_tokens, cache_write_tokens, image_units
				) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
				ON CONFLICT(provider_model) DO UPDATE SET
					error_count = error_count + excluded.error_count,
					input_tokens = input_tokens + excluded.input_tokens,
					output_tokens = output_tokens + excluded.output_tokens,
					total_tokens = total_tokens + excluded.total_tokens,
					cache_read_tokens = cache_read_tokens + excluded.cache_read_tokens,
					cache_write_tokens = cache_write_tokens + excluded.cache_write_tokens,
					image_units = image_units + excluded.image_units
			]])
			if not stmt then
				error("failed to prepare stats insert statement")
			end

			lib.finally(function()
				for key, entry in pairs(ctx.stats) do
					stmt:bind_values(
						key,
						entry.error_count,
						entry.input_tokens,
						entry.output_tokens,
						entry.total_tokens,
						entry.cache_read_tokens,
						entry.cache_write_tokens,
						entry.image_units
					)
					stmt:step()
					stmt:reset()
				end
			end, function()
				stmt:finalize()
			end)
		end, function()
			db:close()
		end)
	end)

	if not ok then
		lib.log {
			level = "warning",
			audience = "operator",
			message = "failed to persist llm stats during teardown",
			error = tostring(err),
		}
	end
end

function ExecPrompt(ctx, args, remaining_gen)
	---@cast args LLMExecPromptPayload
	---@cast remaining_gen number

	if ctx.policy.exhausted then
		llm.exhaust()
	end

	args.prompt = lib.rs.filter_text(args.prompt, {
		"NFKC",
		"RmZeroWidth",
		"NormalizeWS",
	})

	if args.prompt == "" then
		lib.rs.user_error {
			causes = { "EMPTY_PROMPT" },
			fatal = false,
			ctx = {},
		}
	end

	local mapped = llm.exec_prompt_transform(args)

	return dispatch_prompt(ctx, mapped, remaining_gen)
end

function ExecPromptTemplate(ctx, args, remaining_gen)
	---@cast args LLMExecPromptTemplatePayload
	---@cast remaining_gen number

	if ctx.policy.exhausted then
		return llm.exhaust()
	end

	-- Template values (principle, criteria, leader and validator answers) reach
	-- the model like a prompt does, so they get the same text normalization
	-- ExecPrompt applies.
	for key, value in pairs(args) do
		if key ~= "template" and type(value) == "string" then
			args[key] = lib.rs.filter_text(value, {
				"NFKC",
				"RmZeroWidth",
				"NormalizeWS",
			})
		end
	end

	local mapped = llm.exec_prompt_template_transform(args)

	return dispatch_prompt(ctx, mapped, remaining_gen)
end
