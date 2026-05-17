# Jarvis System Road Map
*Auto-generated 2026-03-21 19:32. Run `scripts/update_system_road_map.py` to refresh.*

---

## V2 Database Summary

| Database | Tables | Total Rows (approx) |
|---|---|---|
| `agents.db` | 14 | 4,660 |
| `conversations.db` | 10 | 36,995 |
| `core.db` | 9 | 3,318 |
| `intelligence.db` | 26 | 2,161 |
| `journeys.db` | 5 | 77,237 |
| `prompts.db` | 11 | 7,143 |
| `trading_forex.db` | 23 | 68,328 |
| `workspaces.db` | 11 | 11,846 |

---

## agents.db
**Path:** `Database/v2/agents.db`

| Table | Rows | Key Columns |
|---|---|---|
| `agent_activity` | 3,480 | `id` INTEGER, `agent_id` TEXT, `agent_name` TEXT, `agent_type` TEXT, `workspace_id` TEXT, `action_type` TEXT, `details` TEXT, `performance_metrics` TEXT, `timestamp` TIMESTAMP |
| `agent_communication` | 0 | `id` INTEGER, `message_id` TEXT, `sender_agent_id` TEXT, `sender_agent_name` TEXT, `receiver_agent_id` TEXT, `receiver_agent_name` TEXT, `task_id` TEXT, `message_type` TEXT, `content` TEXT, `metadata` TEXT, `timestamp` TEXT, `journey_id` TEXT ... +1 more |
| `agent_compatibility` | 42 | `id` TEXT, `agent_id` TEXT, `compatible_agent_id` TEXT, `compatibility_score` REAL, `notes` TEXT, `created_at` TIMESTAMP |
| `agent_handoffs` | 0 | `id` TEXT, `from_agent_id` TEXT, `to_agent_id` TEXT, `task_id` TEXT, `context` TEXT, `timestamp` TEXT, `status` TEXT |
| `agent_performance` | 98 | `id` TEXT, `agent_id` TEXT, `workspace_id` TEXT, `task_id` TEXT, `success` INTEGER, `completion_time_ms` INTEGER, `quality_score` REAL, `error_count` INTEGER, `timestamp` TEXT, `metadata` TEXT |
| `agent_registry` | 327 | `id` TEXT, `agent_name` TEXT, `agent_type` TEXT, `capabilities` TEXT, `status` TEXT, `created_at` TEXT, `updated_at` TEXT, `model_preference` TEXT, `vault_path` TEXT, `team_id` TEXT |
| `agent_skills` | 296 | `id` TEXT, `agent_id` TEXT, `skill_name` TEXT, `skill_config` TEXT, `proficiency_score` REAL |
| `agent_team_members` | 287 | `team_id` TEXT, `agent_id` TEXT, `role` TEXT, `assigned_at` TEXT |
| `agent_teams` | 1 | `id` TEXT, `team_name` TEXT, `workspace_id` TEXT, `created_at` TEXT, `status` TEXT, `description` TEXT |
| `journey_steps` | 0 | `id` INTEGER, `step_id` TEXT, `journey_id` TEXT, `step_name` TEXT, `description` TEXT, `step_type` TEXT, `input_data` TEXT, `output_data` TEXT, `status` TEXT, `error` TEXT, `timestamp` TEXT |
| `orchestrator_agent_performance` | 3 | `id` INTEGER, `orchestrator_id` TEXT, `agent_id` TEXT, `success_rate` REAL, `avg_response_time` REAL, `total_tasks` INTEGER, `last_updated` TIMESTAMP |
| `orchestrator_capability_mapping` | 122 | `id` INTEGER, `orchestrator_id` TEXT, `capability` TEXT, `handler_name` TEXT, `priority` INTEGER, `created_at` TIMESTAMP, `metadata` TEXT |
| `perplexica_knowledge` | 3 | `id` INTEGER, `query` TEXT, `response` TEXT, `context` TEXT, `relevance_score` REAL, `created_at` TIMESTAMP |
| `ui_patterns` | 1 | `id` INTEGER, `pattern_type` TEXT, `application` TEXT, `ui_element` TEXT, `action_sequence` TEXT, `success_rate` REAL, `usage_count` INTEGER, `created_at` TIMESTAMP, `updated_at` TIMESTAMP |

## conversations.db
**Path:** `Database/v2/conversations.db`

| Table | Rows | Key Columns |
|---|---|---|
| `agent_communications` | 27,269 | `id` TEXT, `from_agent_id` TEXT, `to_agent_id` TEXT, `workspace_id` TEXT, `message` TEXT, `context` TEXT, `timestamp` TEXT, `conversation_id` TEXT |
| `claude_conversations` | 3,076 | `id` INTEGER, `workspace_id` TEXT, `session_id` TEXT, `user_id` TEXT, `user_request` TEXT, `claude_response` TEXT, `conversation_context` TEXT, `timestamp` REAL, `created_at` DATETIME |
| `conversation_links` | 158 | `id` INTEGER, `user_conversation_id` TEXT, `boardroom_conversation_id` TEXT, `journey_id` TEXT, `workspace_id` TEXT, `user_id` TEXT, `link_type` TEXT, `created_at` REAL, `updated_at` REAL, `is_active` INTEGER, `metadata` TEXT |
| `conversation_summaries` | 0 | `id` TEXT, `conversation_id` TEXT, `summary_text` TEXT, `decisions` TEXT, `created_at` TEXT, `compression_level` INTEGER |
| `conversations` | 237 | `id` TEXT, `workspace_id` TEXT, `user_id` TEXT, `title` TEXT, `created_at` TEXT, `updated_at` TEXT, `status` TEXT, `conversation_type` TEXT, `metadata` TEXT |
| `messages` | 156 | `id` TEXT, `conversation_id` TEXT, `role` TEXT, `content` TEXT, `tool_calls` TEXT, `timestamp` TEXT, `token_count` INTEGER, `model_used` TEXT |
| `session_objects` | 10 | `object_id` TEXT, `session_id` TEXT, `object_name` TEXT, `object_type` TEXT, `app_name` TEXT, `status` TEXT, `created_at` TIMESTAMP, `updated_at` TIMESTAMP, `metadata` TEXT |
| `sessions` | 321 | `session_id` TEXT, `user_id` TEXT, `created_at` TIMESTAMP, `updated_at` REAL, `deleted_at` REAL, `archived_at` REAL, `metadata` TEXT |
| `ui_health_log` | 0 | `id` INTEGER, `ts` REAL, `endpoint` TEXT, `method` TEXT, `status_code` INTEGER, `error_msg` TEXT, `duration_ms` INTEGER, `resolved` INTEGER, `created_at` TEXT |
| `workspace_conversations` | 5,768 | `id` INTEGER, `workspace_id` INTEGER, `participant_id` INTEGER, `participant_type` TEXT, `participant_name` TEXT, `message_content` TEXT, `message_type` TEXT, `phase` TEXT, `timestamp` TEXT, `metadata` TEXT |

## core.db
**Path:** `Database/v2/core.db`

| Table | Rows | Key Columns |
|---|---|---|
| `auth_tokens` | 1,471 | `id` TEXT, `user_id` TEXT, `token` TEXT, `expires_at` TEXT, `created_at` TEXT |
| `broker_credentials` | 1 | `id` INTEGER, `user_id` INTEGER, `broker` TEXT, `encrypted_key` BLOB, `key_salt` TEXT, `account_id` TEXT, `environment` TEXT, `base_url` TEXT, `account_currency` TEXT, `account_alias` TEXT, `available_accounts` TEXT, `created_at` TEXT ... +1 more |
| `oauth_states` | 164 | `id` INTEGER, `state_token` TEXT, `user_id` TEXT, `service_name` TEXT, `created_at` DATETIME, `expires_at` DATETIME, `used` BOOLEAN |
| `permissions` | 0 | `id` TEXT, `user_id` TEXT, `resource_type` TEXT, `resource_id` TEXT, `permission_level` TEXT, `granted_by` TEXT, `created_at` TEXT |
| `service_connections` | 7 | `id` INTEGER, `user_id` TEXT, `service_name` TEXT, `connection_status` TEXT, `last_used` DATETIME, `error_message` TEXT, `connection_data` TEXT, `created_at` DATETIME, `updated_at` DATETIME |
| `trading_preferences` | 21 | `id` INTEGER, `user_id` INTEGER, `pref_key` TEXT, `pref_value` TEXT, `updated_at` TEXT |
| `user_credentials` | 7 | `id` INTEGER, `user_id` TEXT, `service_name` TEXT, `encrypted_credentials` TEXT, `created_at` DATETIME, `updated_at` DATETIME, `expires_at` DATETIME, `is_active` BOOLEAN |
| `user_sessions` | 1,643 | `id` INTEGER, `session_id` TEXT, `user_id` INTEGER, `created_at` TEXT, `expires_at` TEXT, `current_workspace_id` TEXT, `workspace_context` TEXT, `conversation_context` TEXT |
| `users` | 4 | `id` TEXT, `username` TEXT, `email` TEXT, `password_hash` TEXT, `is_admin` INTEGER, `created_at` TEXT, `updated_at` TEXT, `preferences` TEXT, `status` TEXT, `trading_team_id` TEXT |

## intelligence.db
**Path:** `Database/v2/intelligence.db`

| Table | Rows | Key Columns |
|---|---|---|
| `collective_knowledge` | 0 | `id` TEXT, `knowledge_type` TEXT, `content` TEXT, `source_agent_id` TEXT, `source_workspace_id` TEXT, `confidence` REAL, `created_at` TEXT, `tags` TEXT |
| `cot_positioning` | 0 | `id` INTEGER, `report_date` TEXT, `currency` TEXT, `spec_long` INTEGER, `spec_short` INTEGER, `spec_net` INTEGER, `asset_mgr_long` INTEGER, `asset_mgr_short` INTEGER, `asset_mgr_net` INTEGER, `combined_net` INTEGER, `combined_net_change` INTEGER, `percentile` REAL ... +3 more |
| `dataset_versions` | 4 | `id` INTEGER, `version_id` TEXT, `parent_version` TEXT, `metadata` TEXT, `dataset_info` TEXT, `performance_metrics` TEXT, `timestamp` DATETIME, `is_active` BOOLEAN |
| `format_history` | 123 | `id` INTEGER, `source` TEXT, `original_text` TEXT, `formatted_intent` TEXT, `confidence` REAL, `metadata` TEXT, `timestamp` DATETIME |
| `gpt_fallbacks` | 127 | `id` INTEGER, `text` TEXT, `response` TEXT, `timestamp` DATETIME, `processed` BOOLEAN, `metadata` TEXT, `intent` TEXT |
| `handler_analysis` | 42 | `id` INTEGER, `handler_name` TEXT, `complexity_score` REAL, `performance_metrics` TEXT, `training_data` BLOB, `recommendations` TEXT, `analysis_metadata` TEXT, `last_updated` DATETIME, `status` TEXT |
| `handler_error_patterns` | 15 | `id` INTEGER, `handler_name` TEXT, `action` TEXT, `error_type` TEXT, `count` INTEGER, `last_occurred` REAL, `workspace_id` TEXT |
| `handler_execution_tracking` | 85 | `id` INTEGER, `handler_name` TEXT, `action` TEXT, `timestamp` REAL, `journey_id` TEXT, `session_id` TEXT, `success` INTEGER, `execution_time` REAL, `workspace_id` TEXT, `error` TEXT, `parameters` TEXT |
| `handler_performance` | 29 | `id` TEXT, `handler_name` TEXT, `success_count` INTEGER, `failure_count` INTEGER, `avg_latency_ms` REAL, `last_used` TEXT, `metadata` TEXT |
| `intelligence_cache` | 13 | `id` INTEGER, `cache_key` TEXT, `category` TEXT, `instrument` TEXT, `data` TEXT, `fetched_at` TEXT, `expires_at` TEXT, `query_used` TEXT |
| `intelligence_packages` | 2 | `id` INTEGER, `window` TEXT, `generated_at` TEXT, `package_version` TEXT, `macro_data` TEXT, `cross_asset_data` TEXT, `cot_data` TEXT, `calendar_data` TEXT, `per_pair_data` TEXT, `correlation_data` TEXT, `risk_factors` TEXT, `package_text` TEXT ... +7 more |
| `intelligence_snapshots_v2` | 1,432 | `id` INTEGER, `timestamp` TEXT, `cycle_id` TEXT, `decision_id` TEXT, `trade_id` TEXT, `instrument` TEXT, `base_currency` TEXT, `quote_currency` TEXT, `base_currency_rate` REAL, `quote_currency_rate` REAL, `rate_differential` REAL, `base_inflation` REAL ... +47 more |
| `intent_mappings` | 12 | `id` TEXT, `intent_name` TEXT, `handler_name` TEXT, `confidence_threshold` REAL, `priority` INTEGER, `created_at` TEXT |
| `knowledge_relationships` | 2 | `id` INTEGER, `source_knowledge_id` INTEGER, `target_knowledge_id` INTEGER, `relationship_type` TEXT, `confidence` REAL, `description` TEXT, `created_at` DATETIME, `created_by` TEXT |
| `long_term_knowledge` | 5 | `id` INTEGER, `user_id` TEXT, `workspace_id` TEXT, `knowledge_type` TEXT, `category` TEXT, `title` TEXT, `content` TEXT, `keywords` TEXT, `source` TEXT, `verified` INTEGER, `verified_by` TEXT, `verified_at` DATETIME ... +6 more |
| `metrics` | 67 | `id` INTEGER, `metric_name` TEXT, `metric_value` REAL, `metadata` TEXT, `timestamp` DATETIME |
| `model_metrics` | 2 | `id` INTEGER, `model_version` TEXT, `accuracy` REAL, `loss` REAL, `timestamp` DATETIME |
| `model_predictions` | 64 | `id` INTEGER, `text` TEXT, `predicted_intent` TEXT, `confidence` REAL, `timestamp` DATETIME, `metadata` TEXT |
| `model_storage` | 1 | `id` INTEGER, `model_data` BLOB, `version` TEXT, `metadata` TEXT, `accuracy` REAL, `timestamp` DATETIME, `is_latest` INTEGER |
| `patterns` | 0 | `id` TEXT, `pattern_name` TEXT, `pattern_type` TEXT, `pattern_data` TEXT, `confidence_score` REAL, `discovered_at` TEXT, `source` TEXT |
| `performance_baselines` | 2 | `metric_name` TEXT, `average_value` REAL, `median_value` REAL, `percentile_95` REAL, `percentile_99` REAL, `sample_count` INTEGER, `last_updated` REAL, `confidence_level` REAL |
| `performance_metrics` | 121 | `id` INTEGER, `metric_name` TEXT, `value` REAL, `timestamp` REAL, `workspace_id` TEXT, `system` TEXT, `operation` TEXT, `metadata` TEXT, `created_at` TIMESTAMP |
| `request_log` | 11 | `id` TEXT, `user_id` TEXT, `request_text` TEXT, `intent_classified` TEXT, `handler_routed` TEXT, `model_used` TEXT, `response_text` TEXT, `tool_calls` TEXT, `outcome` TEXT, `latency_ms` INTEGER, `timestamp` TEXT, `trade_id` TEXT ... +4 more |
| `schema_validations` | 2 | `id` INTEGER, `journey_id` TEXT, `schema_name` TEXT, `validation_result` INTEGER, `timestamp` TEXT, `data_summary` TEXT, `errors` TEXT |
| `swarm_consensus_cache` | 0 | `id` INTEGER, `instrument` TEXT, `session` TEXT, `simulation_id` TEXT, `direction` TEXT, `confidence_pct` INTEGER, `bull_case` TEXT, `bear_case` TEXT, `key_debate` TEXT, `minority_pct` INTEGER, `minority_view` TEXT, `risk_events` TEXT ... +7 more |
| `validator_decisions` | 0 | `id` INTEGER, `cycle_id` TEXT, `trade_id` TEXT, `instrument` TEXT, `window` TEXT, `package_id` INTEGER, `verdict` TEXT, `confidence_raw` INTEGER, `confidence_adjusted` INTEGER, `confidence_adjustments` TEXT, `flags` TEXT, `position_size_recommendation` TEXT ... +18 more |

## journeys.db
**Path:** `Database/v2/journeys.db`

| Table | Rows | Key Columns |
|---|---|---|
| `boardroom_decisions` | 0 | `id` TEXT, `session_id` TEXT, `workspace_id` TEXT, `decision_text` TEXT, `reasoning` TEXT, `outcome` TEXT, `linked_tasks` TEXT, `created_at` TEXT |
| `boardroom_sessions` | 0 | `id` TEXT, `workspace_id` TEXT, `trigger_request` TEXT, `participants` TEXT, `deliberation_log` TEXT, `final_decision` TEXT, `created_at` TEXT, `duration_ms` INTEGER |
| `conversation_journey_map` | 19 | `id` INTEGER, `journey_id` TEXT, `session_id` TEXT, `user_id` INTEGER, `created_at` TIMESTAMP |
| `journey_steps` | 72,733 | `id` INTEGER, `journey_id` TEXT, `step_id` TEXT, `step_type` TEXT, `parent_step_id` TEXT, `status` TEXT, `input_data` TEXT, `output_data` TEXT, `metadata` TEXT, `error` TEXT, `start_time` TIMESTAMP, `end_time` TIMESTAMP ... +1 more |
| `request_journeys` | 4,485 | `id` TEXT, `user_id` TEXT, `workspace_id` TEXT, `request_text` TEXT, `journey_steps` TEXT, `started_at` TEXT, `completed_at` TEXT, `status` TEXT |

## prompts.db
**Path:** `Database/v2/prompts.db`

| Table | Rows | Key Columns |
|---|---|---|
| `agent_compatibility` | 15 | `id` INTEGER, `prompt_id` TEXT, `agent_type` TEXT, `handler_module` TEXT, `compatibility_score` REAL |
| `agent_prompt_pairings` | 7 | `id` TEXT, `agent_id` TEXT, `prompt_id` TEXT, `pairing_score` REAL, `usage_count` INTEGER, `created_at` TIMESTAMP |
| `model_compatibility` | 12 | `id` INTEGER, `prompt_id` TEXT, `model_id` TEXT, `is_preferred` INTEGER, `min_model_version` TEXT, `compatibility_score` REAL |
| `prompt_examples` | 1 | `id` INTEGER, `prompt_id` TEXT, `input` TEXT, `expected_output` TEXT, `context` TEXT |
| `prompt_performance` | 43 | `id` TEXT, `prompt_id` TEXT, `version` TEXT, `avg_tokens` REAL, `expected_response_tokens` REAL, `success_count` INTEGER, `failure_count` INTEGER, `avg_response_time` REAL, `usage_count` INTEGER, `last_used_at` REAL, `quality_score` REAL |
| `prompt_registry` | 1,171 | `id` TEXT, `prompt_id` TEXT, `name` TEXT, `description` TEXT, `current_version` TEXT, `created_at` REAL, `updated_at` REAL, `author` TEXT, `prompt_family` TEXT, `metadata` TEXT, `is_active` INTEGER |
| `prompt_relationships` | 2 | `id` INTEGER, `source_prompt_id` TEXT, `target_prompt_id` TEXT, `relationship_type` TEXT, `description` TEXT, `created_at` REAL |
| `prompt_tags` | 4,625 | `id` INTEGER, `prompt_id` TEXT, `tag` TEXT |
| `prompt_variables` | 42 | `id` INTEGER, `prompt_id` TEXT, `variable_name` TEXT, `description` TEXT, `is_required` INTEGER, `variable_type` TEXT, `default_value` TEXT |
| `prompt_versions` | 1,178 | `id` TEXT, `prompt_id` TEXT, `version` TEXT, `content` TEXT, `created_at` REAL, `author` TEXT, `changelog` TEXT, `is_active` INTEGER |
| `usage_context` | 47 | `id` INTEGER, `prompt_id` TEXT, `context_type` TEXT, `workflow_stage` TEXT |

## trading_forex.db
**Path:** `Database/v2/trading_forex.db`

| Table | Rows | Key Columns |
|---|---|---|
| `backtest_metadata` | 9 | `key` TEXT, `value` TEXT |
| `backtest_setup_performance` | 46,192 | `setup` TEXT, `pair` TEXT, `timeframe` TEXT, `regime` TEXT, `trade_count` INTEGER, `win_count` INTEGER, `win_rate` REAL, `total_pips` REAL, `avg_pips` REAL, `profit_factor` REAL, `avg_risk_reward` REAL, `max_favorable` REAL ... +6 more |
| `backtest_trades_ema` | 25 | `id` INTEGER, `trade_id` INTEGER, `pair` TEXT, `entry_time` TEXT, `exit_time` TEXT, `entry_price` REAL, `exit_price` REAL, `direction` TEXT, `setup` TEXT, `result` TEXT, `pips` REAL, `regime` TEXT ... +16 more |
| `combined_playbook` | 37 | `id` TEXT, `pair` TEXT, `direction` TEXT, `play_type` TEXT, `fan_width_min` REAL, `fan_width_max` REAL, `thesis_trades` INTEGER, `thesis_wr` REAL, `thesis_pf` REAL, `snipe_required` INTEGER, `snipe_direction` TEXT, `snipe_phase` TEXT ... +9 more |
| `exit_learning` | 105 | `id` INTEGER, `trade_id` TEXT, `user_id` INTEGER, `setup_name` TEXT, `pair` TEXT, `direction` TEXT, `regime` TEXT, `entry_type` TEXT, `entry_price` REAL, `initial_sl` REAL, `initial_tp` REAL, `initial_rr_target` REAL ... +29 more |
| `live_trades` | 74 | `id` TEXT, `pair` TEXT, `direction` TEXT, `entry_price` REAL, `exit_price` REAL, `pnl_pips` REAL, `pnl_usd` REAL, `setup_code` TEXT, `regime` TEXT, `entry_time` TEXT, `exit_time` TEXT, `status` TEXT ... +29 more |
| `manual_trade_analysis` | 84 | `id` INTEGER, `trade_id` TEXT, `pair` TEXT, `direction` TEXT, `entry_time` TEXT, `exit_time` TEXT, `pips` REAL, `result` TEXT, `entry_price` REAL, `exit_price` REAL, `setup_type` TEXT, `fan_state` TEXT ... +12 more |
| `market_snapshots` | 0 | `id` TEXT, `pair` TEXT, `timeframe` TEXT, `timestamp` TEXT, `open` REAL, `high` REAL, `low` REAL, `close` REAL, `volume` REAL, `indicators` TEXT |
| `scout_alerts` | 5,325 | `id` TEXT, `pair` TEXT, `setup_code` TEXT, `alert_type` TEXT, `sniper_score` REAL, `conditions` TEXT, `timestamp` TEXT, `status` TEXT, `setup_name` TEXT, `score` INTEGER, `direction` TEXT, `historical_win_rate` REAL ... +27 more |
| `scout_findings` | 1,884 | `id` INTEGER, `timestamp` TEXT, `pair` TEXT, `setup_type` TEXT, `setup_name` TEXT, `direction` TEXT, `entry_price` REAL, `scout_confidence` REAL, `session_quality` REAL, `market_conditions` TEXT, `sniper_score` REAL, `historical_win_rate` REAL ... +32 more |
| `scout_performance_analytics` | 8 | `id` INTEGER, `pair` TEXT, `setup_type` TEXT, `ema_fan_state` TEXT, `session_quality_range` TEXT, `total_findings` INTEGER, `triggered_findings` INTEGER, `successful_findings` INTEGER, `trigger_rate` REAL, `success_rate` REAL, `avg_pips` REAL, `confidence_accuracy` REAL ... +4 more |
| `scout_profile_findings` | 948 | `id` INTEGER, `timestamp` TEXT, `profile_key` TEXT, `pair` TEXT, `direction` TEXT, `entry_price` REAL, `rsi` REAL, `stoch_k` REAL, `regime` TEXT, `session` TEXT, `candle_pattern` TEXT, `bb_zone` TEXT ... +13 more |
| `scout_profile_live_stats` | 321 | `profile_key` TEXT, `pair` TEXT, `total_findings` INTEGER, `wins` INTEGER, `losses` INTEGER, `neutral` INTEGER, `live_win_rate` REAL, `avg_pips` REAL, `last_updated` TEXT, `last_7d_wins` INTEGER, `last_7d_total` INTEGER, `last_7d_win_rate` REAL |
| `scout_profile_meta` | 1 | `key` TEXT, `value` TEXT |
| `setup_revenue` | 62 | `id` INTEGER, `setup_name` TEXT, `pair` TEXT, `total_trades` INTEGER, `wins` INTEGER, `losses` INTEGER, `total_pips` REAL, `total_usd` REAL, `best_trade_usd` REAL, `worst_trade_usd` REAL, `avg_r_multiple` REAL, `avg_duration_minutes` REAL ... +8 more |
| `setup_trades` | 161 | `id` INTEGER, `trade_id` TEXT, `setup_name` TEXT, `pair` TEXT, `direction` TEXT, `entry_price` REAL, `exit_price` REAL, `stop_loss` REAL, `take_profit` REAL, `units` REAL, `pnl_pips` REAL, `pnl_usd` REAL ... +10 more |
| `strategies` | 0 | `id` TEXT, `name` TEXT, `description` TEXT, `parameters` TEXT, `backtest_results` TEXT, `status` TEXT, `created_at` TEXT |
| `thesis_backtest_performance` | 24 | `pair` TEXT, `direction` TEXT, `total_trades` INTEGER, `wins` INTEGER, `win_rate` REAL, `total_pips` REAL, `avg_pips` REAL, `avg_mfe` REAL, `avg_hold_bars` REAL, `profit_factor` REAL |
| `thesis_backtest_trades` | 9,413 | `id` INTEGER, `pair` TEXT, `direction` TEXT, `entry_time` TEXT, `exit_time` TEXT, `entry_price` REAL, `pips` REAL, `result` TEXT, `exit_reason` TEXT, `mfe_pips` REAL, `rsi_at_entry` REAL, `bb_delta_5bar` REAL ... +3 more |
| `trade_decisions` | 2,127 | `id` TEXT, `pair` TEXT, `setup_code` TEXT, `direction` TEXT, `sniper_score` REAL, `thesis_score` REAL, `validator_verdict` TEXT, `final_action` TEXT, `reasoning` TEXT, `timestamp` TEXT, `timeframe` TEXT, `setup` TEXT ... +23 more |
| `user_chart_annotations` | 187 | `id` INTEGER, `user_id` INTEGER, `pair` TEXT, `annotation_type` TEXT, `price` REAL, `direction` TEXT, `note` TEXT, `ema_cross` TEXT, `fan_state` TEXT, `bb_state` TEXT, `timeframe` TEXT, `active` INTEGER ... +4 more |
| `user_snipe_list` | 54 | `id` INTEGER, `setup_name` TEXT, `pair` TEXT, `direction` TEXT, `source` TEXT, `lifetime_pnl_usd` REAL, `lifetime_trades` INTEGER, `lifetime_win_rate` REAL, `is_active` INTEGER, `promoted_at` TEXT, `demoted_at` TEXT, `notes` TEXT ... +1 more |
| `watch_suggestions` | 1,287 | `id` INTEGER, `cycle_id` TEXT, `instrument` TEXT, `suggestion_type` TEXT, `conditions` TEXT, `raw_suggestion` TEXT, `validator_verdict` TEXT, `validator_confidence` REAL, `created_at` TEXT, `expires_at` TEXT, `last_checked_at` TEXT, `check_count` INTEGER ... +15 more |

## workspaces.db
**Path:** `Database/v2/workspaces.db`

| Table | Rows | Key Columns |
|---|---|---|
| `team_productivity` | 6 | `id` INTEGER, `workspace_id` INTEGER, `metric_name` TEXT, `metric_value` REAL, `recorded_at` TIMESTAMP, `metadata` TEXT |
| `workspace_agent_assignments` | 118 | `id` INTEGER, `workspace_id` INTEGER, `agent_id` TEXT, `agent_name` TEXT, `agent_type` TEXT, `role` TEXT, `assigned_at` TIMESTAMP, `assigned_by` TEXT, `status` TEXT, `metadata` TEXT |
| `workspace_relationships` | 7 | `id` INTEGER, `parent_workspace_id` INTEGER, `child_workspace_id` INTEGER, `relationship_type` TEXT, `created_at` TIMESTAMP, `metadata` TEXT |
| `workspace_routing` | 913 | `workspace_id` TEXT, `shard_file_path` TEXT, `created_at` TEXT |
| `workspace_shares` | 0 | `id` TEXT, `workspace_id` TEXT, `user_id` TEXT, `permission_level` TEXT, `invited_by` TEXT, `created_at` TEXT |
| `workspace_task_comments` | 6,836 | `id` INTEGER, `task_id` INTEGER, `author_id` TEXT, `author_type` TEXT, `content` TEXT, `technical_details` TEXT, `created_at` TIMESTAMP |
| `workspace_task_dependencies` | 13 | `id` INTEGER, `task_id` INTEGER, `depends_on_task_id` INTEGER, `created_at` TIMESTAMP |
| `workspace_task_history` | 280 | `id` INTEGER, `task_id` INTEGER, `action` TEXT, `previous_status` TEXT, `new_status` TEXT, `performed_by` TEXT, `performed_at` TIMESTAMP, `details` TEXT |
| `workspace_tasks` | 2,330 | `id` INTEGER, `workspace_id` INTEGER, `title` TEXT, `description` TEXT, `status` TEXT, `priority` TEXT, `assigned_agent_id` TEXT, `assigned_user_id` INTEGER, `due_date` TEXT, `created_at` TIMESTAMP, `updated_at` TIMESTAMP, `completed_at` TIMESTAMP ... +2 more |
| `workspace_templates` | 0 | `id` TEXT, `name` TEXT, `description` TEXT, `default_agents` TEXT, `default_tools` TEXT |
| `workspaces` | 1,343 | `id` TEXT, `name` TEXT, `description` TEXT, `owner_id` TEXT, `shard_id` TEXT, `created_at` TEXT, `updated_at` TEXT, `status` TEXT, `workspace_type` TEXT, `metadata` TEXT |

---

## Non-V2 Databases

### Trade P&L / Flight Log
**Path:** `Forex Trading Team/Data/trade_log.db`

**NOTE:** `manual_trades` is where actual win/loss results are recorded by the position guardian. `live_trades` in this DB does NOT contain reliable outcome data — **always use `manual_trades` for P&L analysis.**

| Table | Rows | Key Columns |
|---|---|---|
| `live_trades` | 0 | `id` INTEGER, `trade_id` INTEGER, `instrument` TEXT, `direction` TEXT, `entry_price` REAL, `exit_price` REAL, `entry_time` TEXT, `exit_time` TEXT, `result` TEXT, `pips` REAL, `pl_usd` REAL, `rsi` REAL ... +17 more |
| `manual_trades` | 81 | `id` INTEGER, `trade_id` TEXT, `user_id` INTEGER, `pair` TEXT, `direction` TEXT, `units` INTEGER, `entry_price` REAL, `entry_time` TEXT, `exit_price` REAL, `exit_time` TEXT, `result` TEXT, `pips` REAL ... +27 more |
| `mcp_query_log` | 180 | `id` INTEGER, `cycle_id` TEXT, `instrument` TEXT, `timestamp` TEXT, `source` TEXT, `query_type` TEXT, `response_summary` TEXT, `impact_on_decision` TEXT, `error` TEXT |
| `position_monitor_log` | 0 | `id` INTEGER, `timestamp` TEXT, `trade_id` INTEGER, `pair` TEXT, `phase` TEXT, `chart_path` TEXT, `observation` TEXT, `action` TEXT, `candles_watched` INTEGER, `model_used` TEXT, `api_cost` REAL, `created_at` TEXT |
| `setup_revenue` | 0 | `id` INTEGER, `setup_name` TEXT, `pair` TEXT, `total_trades` INTEGER, `wins` INTEGER, `losses` INTEGER, `total_pips` REAL, `total_usd` REAL, `best_trade_usd` REAL, `worst_trade_usd` REAL, `avg_r_multiple` REAL, `avg_duration_minutes` REAL ... +8 more |
| `setup_trades` | 0 | `id` INTEGER, `trade_id` TEXT, `setup_name` TEXT, `pair` TEXT, `direction` TEXT, `entry_price` REAL, `exit_price` REAL, `stop_loss` REAL, `take_profit` REAL, `units` REAL, `pnl_pips` REAL, `pnl_usd` REAL ... +10 more |
| `signal_log` | 1,299 | `id` INTEGER, `cycle_id` TEXT, `instrument` TEXT, `timeframe` TEXT, `timestamp` TEXT, `confluence_score` REAL, `direction` TEXT, `action` TEXT, `indicator_values` TEXT, `patterns_detected` TEXT, `news_sentiment` REAL, `intelligence_summary` TEXT ... +3 more |
| `trade_log` | 13 | `id` INTEGER, `cycle_id` TEXT, `trade_id` TEXT, `instrument` TEXT, `direction` TEXT, `entry_price` REAL, `exit_price` REAL, `units` INTEGER, `stop_loss` TEXT, `take_profit` TEXT, `realized_pl` REAL, `risk_profile` TEXT ... +9 more |
| `user_snipe_list` | 0 | `id` INTEGER, `setup_name` TEXT, `pair` TEXT, `direction` TEXT, `source` TEXT, `lifetime_pnl_usd` REAL, `lifetime_trades` INTEGER, `lifetime_win_rate` REAL, `is_active` INTEGER, `promoted_at` TEXT, `demoted_at` TEXT, `notes` TEXT ... +1 more |
| `validation_log` | 1,218 | `id` INTEGER, `cycle_id` TEXT, `instrument` TEXT, `timestamp` TEXT, `gate1_passed` INTEGER, `gate1_confidence` REAL, `gate1_issues` TEXT, `gate2_passed` INTEGER, `gate2_issues` TEXT, `contradictions` TEXT, `needs_llm_escalation` INTEGER, `recommendation` TEXT ... +1 more |
| `vision_verdicts` | 1 | `id` INTEGER, `timestamp` TEXT, `pair` TEXT, `alert_id` INTEGER, `verdict` TEXT, `direction` TEXT, `confidence` REAL, `chart_path` TEXT, `indicators_json` TEXT, `reasoning` TEXT, `model_used` TEXT, `api_cost` REAL ... +3 more |

### Flight Recorder
**Path:** `Forex Trading Team/Source/flight_recorder.db`

Per-cycle execution audit log. Records every stage of the trading pipeline (`flight_log`) plus live trade phase transitions (`trade_phases`) and workflow anomalies (`workflow_findings`).

| Table | Rows | Key Columns |
|---|---|---|
| `flight_log` | 2,000 | `id` INTEGER, `timestamp` TEXT, `stage` TEXT, `pair` TEXT, `cycle_id` TEXT, `trade_id` TEXT, `status` TEXT, `duration_ms` REAL, `data` TEXT, `note` TEXT, `missing_fields` TEXT, `user_id` INTEGER |
| `session_metrics` | 3 | `id` INTEGER, `session_date` TEXT, `trades` INTEGER, `wins` INTEGER, `losses` INTEGER, `win_rate` REAL, `total_usd` REAL, `avg_pips` REAL, `scout_alerts` INTEGER, `cycles_run` INTEGER, `exec_failures` INTEGER, `phase3_count` INTEGER ... +3 more |
| `trade_phases` | 306 | `id` INTEGER, `timestamp` TEXT, `trade_id` TEXT, `pair` TEXT, `direction` TEXT, `phase` TEXT, `from_phase` TEXT, `pnl_pips` REAL, `bb_width` REAL, `fan_sep_pips` REAL, `retrace_depth` REAL, `e100_tests` INTEGER ... +3 more |
| `workflow_findings` | 500 | `id` INTEGER, `timestamp` TEXT, `cycle_id` TEXT, `pair` TEXT, `severity` TEXT, `category` TEXT, `message` TEXT, `details` TEXT, `acknowledged` INTEGER |

### Historical Cache
**Path:** `Forex Trading Team/Data/historical_cache.db`

Short-term OHLCV candle cache used by the pipeline to avoid repeated broker API calls.

| Table | Rows | Key Columns |
|---|---|---|
| `candles` | 73 | `broker` TEXT, `instrument` TEXT, `granularity` TEXT, `time` TEXT, `open` REAL, `high` REAL, `low` REAL, `close` REAL, `volume` INTEGER, `complete` INTEGER |

---

## File-Based Stores

### Intents/
**Path:** `Intents`
**File count:** 1
**Recent files:** intents_wolfram.json

### MiroFish/simulation_output/
**Path:** `MiroFish/simulation_output`
**File count:** 1
**Recent files:** forex_sim_march2026_results.md

### knowledge/_index.db
**Path:** `knowledge/_index.db`
**Indexed files:** 417 | **FTS rows:** 499

| File Type | Count |
|---|---|
| `skill` | 149 |
| `agent` | 111 |
| `skill_agent` | 66 |
| `pattern` | 29 |
| `scout_retrospective` | 12 |
| `note` | 9 |
| `agent_definition` | 7 |
| `collective_patterns` | 6 |
| `skill_usage` | 6 |
| `decision` | 4 |
| `profile` | 3 |
| `scout_pattern_learnings` | 3 |
| `workspace` | 3 |
| `boardroom_session` | 2 |
| `daily_note` | 2 |
| `boardroom_decisions` | 1 |
| `learning` | 1 |
| `retrospective` | 1 |
| `scout_tuning_data` | 1 |
| `weekly_note` | 1 |

**FTS5 Search:**
```sql
-- knowledge/_index.db
SELECT path, title FROM fts_content WHERE fts_content MATCH 'your keywords' LIMIT 10;
-- Column filter: title:scout  |  Phrase: "EMA fan"  |  Prefix: scout*
```

---

## Quick Lookup: Where Data Lives

| What you need | DB | Table |
|---|---|---|
| **Actual P&L / win-loss outcomes** | `trade_log.db` (non-V2) | **`manual_trades`** — source of truth for realized results |
| Live / open trades (execution state) | `trading_forex.db` | `live_trades` (outcome fields unreliable — use manual_trades for P&L) |
| Active snipe list | `trading_forex.db` | `user_snipe_list WHERE is_active=1` |
| Scout alerts | `trading_forex.db` | `scout_alerts` |
| Scout findings (resolved) | `trading_forex.db` | `scout_findings` |
| Full boardroom decisions | `trading_forex.db` | `trade_decisions` |
| Latest intelligence briefing | `intelligence.db` | `intelligence_packages` (latest row) |
| Per-trade macro snapshot | `intelligence.db` | `intelligence_snapshots_v2` |
| Chart annotations | `trading_forex.db` | `user_chart_annotations` |
| Watch suggestions | `trading_forex.db` | `watch_suggestions` |
| Setup P&L stats | `trading_forex.db` | `setup_revenue` |
| Backtest stats | `trading_forex.db` | `backtest_setup_performance` |
| Agent roster | `agents.db` | `agent_registry` |
| Vault search (FTS5) | `knowledge/_index.db` | `fts_content` |
| Wolfram query templates | File | `Intents/intents_wolfram.json` |
| MiroFish simulation results | File | `MiroFish/simulation_output/*.md` |

---

## Key Query Reference

```sql
-- trading_forex.db

-- Active snipes
SELECT setup_name, pair, direction, lifetime_win_rate, lifetime_pnl_usd
FROM user_snipe_list WHERE is_active=1 ORDER BY lifetime_win_rate DESC;

-- Recent trade losses
SELECT pair, setup, direction, pnl_pips, pnl_usd, outcome, entry_time
FROM live_trades WHERE outcome='loss' ORDER BY entry_time DESC LIMIT 10;

-- Open trades
SELECT id, pair, direction, entry_price, sl_price, tp_price, entry_time
FROM live_trades WHERE status='open';

-- P&L by setup
SELECT setup, COUNT(*) trades, SUM(pnl_usd) total_usd, AVG(outcome_r) avg_r
FROM live_trades WHERE status='closed' GROUP BY setup ORDER BY total_usd DESC;

-- High-score untriggered scout alerts
SELECT pair, direction, setup_code, sniper_score, historical_win_rate, timestamp
FROM scout_alerts WHERE snipe_triggered=0 AND sniper_score > 70
ORDER BY timestamp DESC LIMIT 10;

-- Active watches with progress
SELECT instrument, suggestion_type, conditions_met_count, conditions_total_count,
       validator_verdict, expires_at
FROM watch_suggestions WHERE status='active' ORDER BY conditions_met_count DESC;

-- Best setups by backtest profit factor (min 30 trades)
SELECT setup, pair, timeframe, win_rate, profit_factor, trade_count, best_session
FROM backtest_setup_performance WHERE trade_count >= 30
ORDER BY profit_factor DESC LIMIT 15;

-- Promoted setups by total USD
SELECT setup_name, pair, total_trades, win_rate, total_usd, avg_r_multiple
FROM setup_revenue WHERE promoted=1 ORDER BY total_usd DESC;

-- Trade decisions: approved but failed
SELECT pair, setup_code, sniper_score, outcome_pips, validator_reasoning
FROM trade_decisions WHERE final_action='EXECUTE' AND outcome='loss'
ORDER BY created_at DESC LIMIT 20;

-- Chart annotations for a pair
SELECT annotation_type, price, direction, note, fan_state, created_at
FROM user_chart_annotations WHERE pair='EUR_USD' AND active=1;

-- Exit learning by setup/reason
SELECT setup_name, exit_reason, AVG(actual_rr) avg_rr, AVG(pnl_pips) avg_pips, COUNT(*) n
FROM exit_learning GROUP BY setup_name, exit_reason ORDER BY n DESC;
```

```sql
-- intelligence.db

-- Latest intelligence briefing
SELECT id, generated_at, mirofish_consensus_received, substr(package_text,1,500)
FROM intelligence_packages ORDER BY generated_at DESC LIMIT 1;

-- Macro snapshots where trading was blocked
SELECT instrument, macro_bias, verdict, confidence, summary, pips_result
FROM intelligence_snapshots_v2 WHERE block_trading=1 ORDER BY created_at DESC LIMIT 10;

-- Intelligence snapshot for a pair
SELECT instrument, macro_bias, verdict, kelly_fraction, pips_result, created_at
FROM intelligence_snapshots_v2 WHERE instrument='EUR_USD' ORDER BY created_at DESC LIMIT 5;
```

```sql
-- knowledge/_index.db (FTS5)

-- Full-text search vault
SELECT path, title FROM fts_content WHERE fts_content MATCH 'EMA fan scout signal' LIMIT 10;

-- Search by file type
SELECT path, title FROM fts_content
  WHERE fts_content MATCH 'loss pattern' AND file_type = 'scout_retrospective' LIMIT 5;

-- Snippet with context
SELECT path, snippet(fts_content, 2, '[', ']', '...', 32) excerpt
FROM fts_content WHERE fts_content MATCH 'validator rejection' LIMIT 5;

-- FTS5 syntax: word1 word2 (AND) | word1 OR word2 | "phrase" | prefix* | col:term
```

---
*Generated by `scripts/update_system_road_map.py` on 2026-03-21 19:32*