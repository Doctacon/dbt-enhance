{{ config(materialized='ephemeral') }}

select * from (
    values
        (1001, 'Acme Studio', 'paid', 12900, 'US'),
        (1002, 'Northstar', 'pending', 8450, 'GB'),
        (1003, 'Paper & Pine', 'paid', 23700, 'CA'),
        (1004, 'Soft Signal', 'refunded', 4900, 'DE'),
        (1005, 'Fieldwork', 'paid', 17800, 'US')
) as orders(order_id, customer, status, total_cents, country)
