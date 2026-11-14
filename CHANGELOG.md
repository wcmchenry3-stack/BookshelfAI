# Changelog

## [0.2.0](https://github.com/wcmchenry3-stack/BookshelfAI/compare/v0.1.0...v0.2.0) (2026-10-27)


### Features

* **ai:** contract — drop scan_usage_daily, legacy scan_quota and the enhanced-credit column ([#540](https://github.com/wcmchenry3-stack/BookshelfAI/issues/540)) ([90299ea](https://github.com/wcmchenry3-stack/BookshelfAI/commit/90299ea231483636ab12951a445f5d6d55ff48ee))
* **ai:** contract step for [#582](https://github.com/wcmchenry3-stack/BookshelfAI/issues/582) — drop retired monthly quota keys, flags and env fields ([#594](https://github.com/wcmchenry3-stack/BookshelfAI/issues/594)) ([aac0e6c](https://github.com/wcmchenry3-stack/BookshelfAI/commit/aac0e6c489518568c10d2c3dc6eec4e83d4ea946))
* **auth:** 15-minute access tokens revoked via users.token_version ([fdb5655](https://github.com/wcmchenry3-stack/BookshelfAI/commit/fdb56559f1341aba396d30d6e1f01d94c8a0a603))
* **auth:** 15-minute access tokens revoked via users.token_version ([#689](https://github.com/wcmchenry3-stack/BookshelfAI/issues/689)) ([ba8d5fe](https://github.com/wcmchenry3-stack/BookshelfAI/commit/ba8d5fe88645864c5726ec7389a7668cd83c346e))
* **auth:** cache Google JWKS with TTL and refresh on unknown kid ([cd6d133](https://github.com/wcmchenry3-stack/BookshelfAI/commit/cd6d13326c620a32f361e54a4085f481146b34a2)), closes [#691](https://github.com/wcmchenry3-stack/BookshelfAI/issues/691)
* **auth:** match users by google_id and gate the dev test login ([7a70e1f](https://github.com/wcmchenry3-stack/BookshelfAI/commit/7a70e1f07e3ec9d2f0b7e6f9e4c01e01312a059c))
* **auth:** match users by google_id and gate the dev test login ([#688](https://github.com/wcmchenry3-stack/BookshelfAI/issues/688)) ([dae848f](https://github.com/wcmchenry3-stack/BookshelfAI/commit/dae848ff11a7fa849cdcab9cb835cdab0fba0355))
* **scan:** remove the always-null monthly fields from /scan/usage ([#594](https://github.com/wcmchenry3-stack/BookshelfAI/issues/594)) ([6cd5dbe](https://github.com/wcmchenry3-stack/BookshelfAI/commit/6cd5dbed52cfd18b120c405103a9d8f0d4c30271))
* **sentry:** drop expected client network noise (font-load timeout, bare NetworkError) ([7ddee83](https://github.com/wcmchenry3-stack/BookshelfAI/commit/7ddee8377f8555c62ed710c66e935d65e0503f61))
* **sentry:** drop expected client network noise via narrow beforeSend ([4ca07ae](https://github.com/wcmchenry3-stack/BookshelfAI/commit/4ca07aeb27fb6c8b79466c407390a75d9e803393))


### Bug Fixes

* **app:** keep an advanced book on its destination tab ([99c0e78](https://github.com/wcmchenry3-stack/BookshelfAI/commit/99c0e7872dbe23d3d0000a89b54e6454bf3fe2fc))
* **auth:** case-insensitive email matching, commit retry, conflict recovery ([#688](https://github.com/wcmchenry3-stack/BookshelfAI/issues/688)) ([bfc6637](https://github.com/wcmchenry3-stack/BookshelfAI/commit/bfc6637cb99f93d5121bdaa43a6c97de8380c4bd))
* **auth:** check other holders on case-only email changes too ([#688](https://github.com/wcmchenry3-stack/BookshelfAI/issues/688)) ([02d80b8](https://github.com/wcmchenry3-stack/BookshelfAI/commit/02d80b8c9bad819f8b32817aaf5a167240683c39))
* **auth:** handle login and font-load failures without unhandled rejections ([0d251e8](https://github.com/wcmchenry3-stack/BookshelfAI/commit/0d251e838d21de02112c72227a8641fb9a9f5ec9))
* **auth:** handle login and font-load failures without unhandled rejections ([76b1661](https://github.com/wcmchenry3-stack/BookshelfAI/commit/76b16617378b1edfa14c9e35e1fcae48fbad497a))
* **auth:** harden JWKS cache against malformed responses and cold failures ([0ea1257](https://github.com/wcmchenry3-stack/BookshelfAI/commit/0ea125785c80f9d590841a7416e23804d2cfd01b))
* **auth:** hide DB bind parameters in errors; lock rows in conflict runbook ([#688](https://github.com/wcmchenry3-stack/BookshelfAI/issues/688)) ([4116fec](https://github.com/wcmchenry3-stack/BookshelfAI/commit/4116fec83767a177286478b08a19d77ee0930643))
* **auth:** retry only unique violations, safer conflict runbook ([#688](https://github.com/wcmchenry3-stack/BookshelfAI/issues/688)) ([c95f665](https://github.com/wcmchenry3-stack/BookshelfAI/commit/c95f66572cd7b1b0ba861a767bc05280f60f0440))
* **auth:** review round 1 for token_version ([#689](https://github.com/wcmchenry3-stack/BookshelfAI/issues/689)) ([736d0a1](https://github.com/wcmchenry3-stack/BookshelfAI/commit/736d0a1f2ca08c8ee02582515108f0d84d82ea59))
* **auth:** review round 2 for token_version ([#689](https://github.com/wcmchenry3-stack/BookshelfAI/issues/689)) ([9fc2c92](https://github.com/wcmchenry3-stack/BookshelfAI/commit/9fc2c9272bc474925f2a94afaa69b0fa24187489))
* **ci:** harden destructive migration guard after review ([#687](https://github.com/wcmchenry3-stack/BookshelfAI/issues/687)) ([9470172](https://github.com/wcmchenry3-stack/BookshelfAI/commit/9470172eef81d34c740e68b05f911c5890593a42))
* **ci:** keep every value of a reused local SQL variable ([#687](https://github.com/wcmchenry3-stack/BookshelfAI/issues/687)) ([d5459f9](https://github.com/wcmchenry3-stack/BookshelfAI/commit/d5459f987c079dd8621a80b445f97cbaf9a43829))
* **ci:** resolve local SQL variables, narrow DELETE, report on main PRs ([#687](https://github.com/wcmchenry3-stack/BookshelfAI/issues/687)) ([b9425c9](https://github.com/wcmchenry3-stack/BookshelfAI/commit/b9425c9e00ccb356c60f57499b45cd2d7076db19))
* **cleanup:** also retry refused/reset connections (OSError) in daily cleanup ([9fcd1e7](https://github.com/wcmchenry3-stack/BookshelfAI/commit/9fcd1e7f0b31616d624c145684eb148ed885accf))
* **cleanup:** retry OSError (refused/reset connections) in purge steps ([35de835](https://github.com/wcmchenry3-stack/BookshelfAI/commit/35de835daa7c55cb7cc0b322b78182bbaf48b304)), closes [#675](https://github.com/wcmchenry3-stack/BookshelfAI/issues/675)
* **cleanup:** retry transient DB connection errors in daily cleanup ([#661](https://github.com/wcmchenry3-stack/BookshelfAI/issues/661)) ([9e22e02](https://github.com/wcmchenry3-stack/BookshelfAI/commit/9e22e020f2623a2860b660d75ed8f7ba9414dd0c))
* **config:** never print configuration values from config_check or boot errors ([5e95b77](https://github.com/wcmchenry3-stack/BookshelfAI/commit/5e95b77fd18fe2d407508d7823b03eceac1ebdce))
* **config:** respect source precedence in blank guard; check Turnstile in config_check ([4861886](https://github.com/wcmchenry3-stack/BookshelfAI/commit/4861886008fd33c8ed32cde313ebc3a2b202acf8))
* **config:** treat blank settings as unset and add config_check command ([940e9ee](https://github.com/wcmchenry3-stack/BookshelfAI/commit/940e9ee0aee72b1bfd238671b325d232d9557e57))
* **ios:** fail fast when Xcode Cloud rolls onto an Xcode the Expo SDK can't build ([#708](https://github.com/wcmchenry3-stack/BookshelfAI/issues/708)) ([f2e1086](https://github.com/wcmchenry3-stack/BookshelfAI/commit/f2e108695f52ecd42f56364c11c548e0936c516a))
* **ios:** harden the Xcode Cloud Node install and Xcode guard (review round 1) ([2fa42d2](https://github.com/wcmchenry3-stack/BookshelfAI/commit/2fa42d210813c3e365c50193ca31076b25ab063f))
* **ios:** install Node from nodejs.org instead of Homebrew in Xcode Cloud ([#718](https://github.com/wcmchenry3-stack/BookshelfAI/issues/718)) ([4ddccef](https://github.com/wcmchenry3-stack/BookshelfAI/commit/4ddccef0cfff2a9bfd3bbdb69665b16a1fe67766))
* **ios:** unblock Xcode Cloud: Node from nodejs.org ([#718](https://github.com/wcmchenry3-stack/BookshelfAI/issues/718)) and fail fast on Xcode 27 ([#708](https://github.com/wcmchenry3-stack/BookshelfAI/issues/708)) ([a630558](https://github.com/wcmchenry3-stack/BookshelfAI/commit/a6305588e495e3de131cf74138157553c5c35294))
* **sentry:** match real app:///entry-&lt;hash&gt;.js frames in font-timeout filter ([c49e947](https://github.com/wcmchenry3-stack/BookshelfAI/commit/c49e947c3170d4f16e07e443d2e0b8ee7701eb97))
* **sentry:** tighten noise filter per review and document its limits ([0c9cd41](https://github.com/wcmchenry3-stack/BookshelfAI/commit/0c9cd410e01db5007a550703bd1c729521dab133))
