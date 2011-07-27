import sys
import types

import newrelic.api.transaction
import newrelic.api.name_transaction
import newrelic.api.function_trace
import newrelic.api.error_trace
import newrelic.api.object_wrapper
import newrelic.api.import_hook

def name_controller(self, environ, start_response):
    action = environ['pylons.routes_dict']['action']
    return "%s.%s" % (newrelic.api.object_wrapper.callable_name(self), action)

class capture_error(newrelic.api.object_wrapper.ObjectWrapper):
    def __call__(self, controller, func, args):
        current_transaction = newrelic.api.transaction.transaction()
        if current_transaction:
            webob_exc = newrelic.api.import_hook.import_module('webob.exc')
            try:
                return self.__next_object__(controller, func, args)
            except webob_exc.HTTPException:
                raise
            except:
                current_transaction.notice_error(*sys.exc_info())
                raise
        else:
            return self.__next_object__(controller, func, args)

def instrument(module):

    if module.__name__ == 'pylons.wsgiapp':
        newrelic.api.error_trace.wrap_error_trace(module, 'PylonsApp.__call__')

    elif module.__name__ == 'pylons.controllers.core':
        newrelic.api.name_transaction.wrap_name_transaction(
                module, 'WSGIController.__call__', name_controller)
        newrelic.api.function_trace.wrap_function_trace(
                module, 'WSGIController.__call__')
        newrelic.api.function_trace.wrap_function_trace(
                module, 'WSGIController._perform_call',
                (lambda self, func, args: newrelic.api.object_wrapper.callable_name(func)))
        newrelic.api.object_wrapper.wrap_object(
                module, 'WSGIController._perform_call', capture_error)

    elif module.__name__ == 'pylons.templating':

        newrelic.api.function_trace.wrap_function_trace(module, 'render_genshi')
        newrelic.api.function_trace.wrap_function_trace(module, 'render_mako')
        newrelic.api.function_trace.wrap_function_trace(module, 'render_jinja2')
