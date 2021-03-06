import importlib.util
import warnings

from django.apps import apps
from django.conf.urls import include, url

from pretix.presale.urls import (
    event_patterns, locale_patterns, organizer_patterns,
)
from pretix.urls import common_patterns

presale_patterns = [
    url(r'', include(locale_patterns + [
        url(r'^(?P<event>[^/]+)/', include(event_patterns)),
        url(r'', include(organizer_patterns))
    ], namespace='presale'))
]

raw_plugin_patterns = []
for app in apps.get_app_configs():
    if hasattr(app, 'PretixPluginMeta'):
        if importlib.util.find_spec(app.name + '.urls'):
            urlmod = importlib.import_module(app.name + '.urls')
            if hasattr(urlmod, 'event_patterns'):
                raw_plugin_patterns.append(
                    url(r'^(?P<event>[^/]+)/', include(urlmod.event_patterns, namespace=app.label))
                )
        elif importlib.util.find_spec(app.name + '.subdomain_urls'):
            warnings.warn('Please put your config in an \'urls\' module using the event_patterns '
                          'attribute. Support for subdomain_urls in plugins will be dropped in the future.',
                          DeprecationWarning)
            urlmod = importlib.import_module(app.name + '.subdomain_urls')
            raw_plugin_patterns.append(
                url(r'', include(urlmod, namespace=app.label))
            )

plugin_patterns = [
    url(r'', include(raw_plugin_patterns, namespace='plugins'))
]

# The presale namespace comes last, because it contains a wildcard catch
urlpatterns = common_patterns + plugin_patterns + presale_patterns
