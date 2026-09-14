# Order service architecture

The Web client (`web`) sends HTTPS requests to the Orders API (`api`). The Orders API reads and writes order records in the Orders database (`database`). The client has no direct database connection.

Use the following exact `id` and `value` attributes for the three vertex cells. The display label is the value, not the ID.

| `id` | `value` (display label) |
| --- | --- |
| `web` | `Web client` |
| `api` | `Orders API` |
| `database` | `Orders database` |

Add directed edges `web_to_api` from `web` to `api` and `api_to_database` from `api` to `database`. Keep the diagram flat, with structural cells `0` and `1`, and all vertices and edges parented to `1`. Return one native, uncompressed draw.io page. Omit vertex geometry so the converter automatically lays out the repaired diagram. Use plain-text labels and omit optional styles and custom metadata. Do not include external images, links, fonts, scripts, groups or additional components.
