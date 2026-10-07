# Run 67 vs run 63

### gpt-5.6-luna, fill 0.9 (100 rows): facts / acc1 / seed$ (DQ seeds)

| row | run 67 (core 1.20) | run 63 (core 1.16) |
|---|---|---|
| none | 53.0 / 100% / $0.068 | 53.0 / 100% / $0.067 |
| tool_and_user_summary_anchored | 53.0 / 100% / $0.073 | 53.0 / 100% / $0.075 |
| tool_summary_anchored | 53.0 / 100% / $0.070 | 53.0 / 100% / $0.074 |
| user_summary_anchored | 53.0 / 100% / $0.085 | 53.0 / 97% / $0.087 |
| anchored_min_gain | 53.0 / 94% / $0.078 | 53.0 / 98% / $0.077 |
| anchored | 53.0 / 96% / $0.075 | 50.2 / 92% / $0.078 |
| anchored_no_assistant | 51.6 / 97% / $0.076 | 50.2 / 94% / $0.078 |
| summarization | 19.8 / 38% / $0.138 | 18.4 / 36% / $0.115 |
| token_budget_summarize | 29.0 / 46% / $0.093 | 32.2 / 61% / $0.077 |
| context_window | 26.8 / 52% / $0.090 | 42.6 / 80% / $0.092 |
| context_window_lazy | 53.0 / 95% / $0.094 | 53.0 / 100% / $0.090 |
| context_window_aggressive | 20.2 / 39% / $0.066 | 22.4 / 43% / $0.067 |
| tool_result | 49.6 / 90% / $0.095 | 50.2 / 90% / $0.093 |
| selective_tool_call | 49.8 / 94% / $0.100 | 49.8 / 94% / $0.093 |
| truncation | 34.8 / 66% / $0.076 | 35.4 / 67% / $0.076 |
| sliding_window | 8.0 / 17% / $0.076 | 8.0 / 17% / $0.076 |
| token_budget_fallback | 21.4 / 41% / $0.129 | 21.8 / 42% / $0.143 |
| token_budget_tools_first | 19.0 / 37% / $0.092 | 18.4 / 36% / $0.097 |
| token_budget_truncate_first | 20.2 / 39% / $0.091 | 18.6 / 36% / $0.090 |
| token_budget_window_first | 10.0 / 20% / $0.058 | 11.4 / 23% / $0.057 |

### gpt-5.6-luna, fill 1.5 (100 rows): facts / acc1 / seed$ (DQ seeds)

| row | run 67 (core 1.20) | run 63 (core 1.16) |
|---|---|---|
| none | 53.0 / 97% / $0.150 (5) | 53.0 / 97% / $0.147 (5) |
| tool_and_user_summary_anchored | 53.0 / 94% / $0.143 | 53.0 / 100% / $0.120 |
| tool_summary_anchored | 53.0 / 100% / $0.308 (2) | 53.0 / 94% / $0.234 (1) |
| user_summary_anchored | 53.0 / 97% / $0.221 (5) | 53.0 / 100% / $0.219 (4) |
| anchored_min_gain | 46.0 / 87% / $0.192 | 42.8 / 81% / $0.189 |
| anchored | 39.6 / 75% / $0.185 | 36.0 / 66% / $0.170 |
| anchored_no_assistant | 28.0 / 53% / $0.211 | 32.4 / 61% / $0.195 |
| summarization | 16.6 / 25% / $0.213 | 18.0 / 31% / $0.176 |
| token_budget_summarize | 24.4 / 47% / $0.153 | 21.8 / 42% / $0.198 |
| context_window | 14.6 / 29% / $0.125 | 19.0 / 37% / $0.136 |
| context_window_lazy | 22.0 / 43% / $0.161 | 24.8 / 47% / $0.151 |
| context_window_aggressive | 14.2 / 28% / $0.099 | 15.2 / 30% / $0.100 |
| tool_result | 47.4 / 87% / $0.178 (5) | 44.6 / 84% / $0.196 (5) |
| selective_tool_call | 47.0 / 89% / $0.195 (5) | 43.4 / 82% / $0.185 (5) |
| truncation | 17.8 / 35% / $0.137 | 17.0 / 33% / $0.136 |
| sliding_window | 8.0 / 17% / $0.121 | 8.0 / 17% / $0.123 |
| token_budget_fallback | 18.4 / 36% / $0.263 | 17.4 / 34% / $0.258 |
| token_budget_tools_first | 17.6 / 34% / $0.141 | 18.6 / 36% / $0.139 |
| token_budget_truncate_first | 17.2 / 34% / $0.124 | 17.4 / 34% / $0.130 |
| token_budget_window_first | 10.8 / 22% / $0.083 | 9.6 / 19% / $0.090 |

### gpt-5.6-luna, fill 3.0 (100 rows): facts / acc1 / seed$ (DQ seeds)

| row | run 67 (core 1.20) | run 63 (core 1.16) |
|---|---|---|
| none | 53.0 / 90% / $0.443 (5) | 53.0 / 85% / $0.466 (5) |
| tool_and_user_summary_anchored | 53.0 / 94% / $0.792 (1) | 53.0 / 90% / $0.438 (1) |
| tool_summary_anchored | 53.0 / 97% / $0.941 (5) | 53.0 / 87% / $2.508 (5) |
| user_summary_anchored | 53.0 / 92% / $0.590 (5) | 53.0 / 90% / $0.601 (5) |
| anchored_min_gain | 13.0 / 26% / $0.595 (5) | 13.0 / 26% / $0.579 (5) |
| anchored | 13.0 / 26% / $0.608 (5) | 13.0 / 26% / $0.520 (5) |
| anchored_no_assistant | 25.2 / 46% / $0.360 (5) | 39.8 / 73% / $0.347 (5) |
| summarization | 16.6 / 26% / $0.408 | 16.6 / 31% / $0.334 |
| token_budget_summarize | 20.2 / 36% / $0.309 | 9.6 / 20% / $0.354 |
| context_window | 16.4 / 32% / $0.267 | 17.4 / 34% / $0.243 |
| context_window_lazy | 14.2 / 28% / $0.301 | 11.4 / 23% / $0.319 |
| context_window_aggressive | 3.2 / 8% / $0.151 (3) | 3.4 / 8% / $0.134 (1) |
| tool_result | 43.0 / 76% / $0.493 (5) | 47.4 / 81% / $0.542 (5) |
| selective_tool_call | 44.2 / 70% / $0.522 (5) | 44.6 / 73% / $0.549 (5) |
| truncation | 16.8 / 33% / $0.237 | 17.2 / 34% / $0.240 |
| sliding_window | 8.0 / 17% / $0.236 | 8.0 / 17% / $0.243 |
| token_budget_fallback | 9.2 / 19% / $0.430 | 9.8 / 20% / $0.436 |
| token_budget_tools_first | 8.4 / 17% / $0.193 | 8.8 / 18% / $0.186 |
| token_budget_truncate_first | 9.6 / 20% / $0.193 | 9.2 / 19% / $0.189 |
| token_budget_window_first | 8.0 / 17% / $0.148 | 8.8 / 18% / $0.136 |

### gpt-6-luna, fill 0.9 (100 rows): facts / acc1 / seed$ (DQ seeds)

| row | run 67 (core 1.20) | run 63 (core 1.16) |
|---|---|---|
| none | 53.0 / 100% / $0.036 | 53.0 / 100% / $0.035 |
| tool_and_user_summary_anchored | 53.0 / 100% / $0.035 | 53.0 / 100% / $0.035 |
| tool_summary_anchored | 53.0 / 100% / $0.036 | 53.0 / 100% / $0.035 |
| user_summary_anchored | 53.0 / 99% / $0.044 | 53.0 / 100% / $0.046 |
| anchored_min_gain | 53.0 / 100% / $0.034 | 53.0 / 100% / $0.034 |
| anchored | 47.4 / 88% / $0.037 | 46.2 / 87% / $0.037 |
| anchored_no_assistant | 47.4 / 89% / $0.037 | 48.8 / 91% / $0.037 |
| summarization | 47.4 / 75% / $0.090 | 29.0 / 53% / $0.071 |
| token_budget_summarize | 28.4 / 46% / $0.044 | 33.8 / 64% / $0.037 |
| context_window | 42.2 / 80% / $0.049 | 43.2 / 82% / $0.049 |
| context_window_lazy | 41.8 / 79% / $0.048 | 42.2 / 83% / $0.050 |
| context_window_aggressive | 24.0 / 46% / $0.034 | 19.8 / 39% / $0.034 |
| tool_result | 39.0 / 74% / $0.049 | 44.6 / 84% / $0.049 |
| selective_tool_call | 44.2 / 84% / $0.049 | 39.4 / 74% / $0.049 |
| truncation | 30.6 / 59% / $0.040 | 31.0 / 60% / $0.040 |
| sliding_window | 8.0 / 17% / $0.045 | 8.0 / 17% / $0.045 |
| token_budget_fallback | 23.2 / 45% / $0.074 | 23.0 / 44% / $0.080 |
| token_budget_tools_first | 18.6 / 36% / $0.047 | 18.6 / 36% / $0.047 |
| token_budget_truncate_first | 18.0 / 35% / $0.047 | 18.6 / 36% / $0.048 |
| token_budget_window_first | 15.6 / 31% / $0.027 | 12.6 / 25% / $0.027 |

### gpt-6-luna, fill 1.5 (100 rows): facts / acc1 / seed$ (DQ seeds)

| row | run 67 (core 1.20) | run 63 (core 1.16) |
|---|---|---|
| none | 53.0 / 100% / $0.075 (5) | 53.0 / 99% / $0.075 (5) |
| tool_and_user_summary_anchored | 53.0 / 98% / $0.058 | 53.0 / 100% / $0.058 |
| tool_summary_anchored | 53.0 / 97% / $0.051 | 53.0 / 100% / $0.049 |
| user_summary_anchored | 53.0 / 96% / $0.111 (5) | 53.0 / 100% / $0.114 (5) |
| anchored_min_gain | 27.6 / 54% / $0.092 | 24.8 / 47% / $0.093 |
| anchored | 31.4 / 59% / $0.094 | 26.0 / 50% / $0.096 |
| anchored_no_assistant | 28.6 / 55% / $0.093 | 35.0 / 67% / $0.096 |
| summarization | 38.4 / 74% / $0.132 | 24.6 / 43% / $0.105 |
| token_budget_summarize | 33.8 / 61% / $0.081 | 16.0 / 32% / $0.096 |
| context_window | 21.8 / 42% / $0.064 | 21.0 / 41% / $0.064 |
| context_window_lazy | 22.0 / 41% / $0.078 | 25.8 / 50% / $0.078 |
| context_window_aggressive | 9.8 / 20% / $0.050 | 9.2 / 19% / $0.052 |
| tool_result | 39.0 / 71% / $0.093 (5) | 44.6 / 84% / $0.104 (5) |
| selective_tool_call | 40.2 / 76% / $0.098 (5) | 40.2 / 75% / $0.098 (5) |
| truncation | 18.6 / 36% / $0.067 | 20.2 / 39% / $0.064 |
| sliding_window | 8.0 / 17% / $0.072 | 8.0 / 17% / $0.072 |
| token_budget_fallback | 17.4 / 34% / $0.128 | 17.8 / 35% / $0.134 |
| token_budget_tools_first | 13.6 / 27% / $0.066 | 14.6 / 29% / $0.068 |
| token_budget_truncate_first | 16.0 / 32% / $0.070 | 15.2 / 31% / $0.066 |
| token_budget_window_first | 9.6 / 20% / $0.045 | 9.8 / 20% / $0.045 |

### gpt-6-luna, fill 3.0 (100 rows): facts / acc1 / seed$ (DQ seeds)

| row | run 67 (core 1.20) | run 63 (core 1.16) |
|---|---|---|
| none | 53.0 / 84% / $0.313 (5) | 53.0 / 87% / $0.226 (5) |
| tool_and_user_summary_anchored | 53.0 / 100% / $0.109 | 53.0 / 100% / $0.205 (1) |
| tool_summary_anchored | 53.0 / 97% / $0.310 (5) | 53.0 / 84% / $1.290 (5) |
| user_summary_anchored | 53.0 / 89% / $0.301 (5) | 53.0 / 85% / $0.301 (5) |
| anchored_min_gain | 13.0 / 26% / $0.349 (5) | 13.2 / 26% / $0.313 (5) |
| anchored | 13.0 / 26% / $0.326 (5) | 13.0 / 26% / $0.318 (5) |
| anchored_no_assistant | 25.0 / 52% / $0.309 (5) | 21.4 / 39% / $0.193 (5) |
| summarization | 30.4 / 31% / $0.253 | 29.6 / 50% / $0.203 |
| token_budget_summarize | 44.0 / 74% / $0.167 | 9.2 / 19% / $0.183 |
| context_window | 13.4 / 30% / $0.122 | 17.2 / 34% / $0.122 |
| context_window_lazy | 15.0 / 30% / $0.165 | 14.2 / 29% / $0.167 |
| context_window_aggressive | 0.0 / 2% / $0.078 (1) | 0.2 / 2% / $0.098 (4) |
| tool_result | 39.0 / 71% / $0.305 (5) | 41.8 / 72% / $0.270 (5) |
| selective_tool_call | 41.0 / 70% / $0.290 (5) | 40.6 / 75% / $0.265 (5) |
| truncation | 15.4 / 30% / $0.121 | 14.4 / 29% / $0.118 |
| sliding_window | 8.0 / 17% / $0.140 | 8.0 / 17% / $0.140 |
| token_budget_fallback | 8.8 / 18% / $0.240 | 8.0 / 17% / $0.245 |
| token_budget_tools_first | 8.0 / 17% / $0.095 | 8.0 / 17% / $0.100 |
| token_budget_truncate_first | 8.0 / 17% / $0.094 | 8.0 / 17% / $0.102 |
| token_budget_window_first | 8.0 / 17% / $0.075 | 8.0 / 17% / $0.075 |

run 67 seed$ total $126.85; rate-limit retries 1812; row errors 0
