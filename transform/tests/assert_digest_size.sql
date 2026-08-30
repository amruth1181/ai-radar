-- The digest must fit in one message. Discord allows 10 embeds per message and the
-- 4096-char embed description is sized for roughly a dozen items; well past that the
-- message splits and stops being a single glance.

select count(*) as digest_rows
from {{ ref('fct_daily_digest') }}
having count(*) > {{ var('digest_size', 12) }}
