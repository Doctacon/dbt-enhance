{% macro dollars(column) %}
    round({{ column }} / 100.0, 2)
{% endmacro %}
