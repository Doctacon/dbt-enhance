-- Select this query in Helix and press Space, x.
-- The ephemeral ref is compiled to a CTE; no dbt build is needed.
select
    order_id,
    customer,
    status,
    {{ dollars('total_cents') }} as total_usd,
    country,
    '{{ target.name }}' as dbt_target
from {{ ref('orders') }}
order by order_id
