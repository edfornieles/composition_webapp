from django import template

register = template.Library()


@register.filter
def underscored(value):
    return str(value).replace(" ", "_")
