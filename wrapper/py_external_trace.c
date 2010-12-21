/* ------------------------------------------------------------------------- */

/* (C) Copyright 2010 New Relic Inc. All rights reserved. */

/* ------------------------------------------------------------------------- */

#include "py_external_trace.h"

#include "globals.h"
#include "logging.h"

#include "web_transaction_funcs.h"

/* ------------------------------------------------------------------------- */

#ifndef PyVarObject_HEAD_INIT
#define PyVarObject_HEAD_INIT(type, size) PyObject_HEAD_INIT(type) size,
#endif

/* ------------------------------------------------------------------------- */

NRExternalTraceObject *NRExternalTrace_New(nr_web_transaction *transaction,
                                           const char *url)
{
    NRExternalTraceObject *self;

    self = PyObject_New(NRExternalTraceObject, &NRExternalTrace_Type);
    if (self == NULL)
        return NULL;

    if (transaction) {
        self->transaction_trace = nr_web_transaction__allocate_external_node(
                transaction, url);
    }
    else
        self->transaction_trace = NULL;

    self->outer_transaction = NULL;

    return self;
}

static void NRExternalTrace_dealloc(NRExternalTraceObject *self)
{
    PyObject_Del(self);
}

static PyObject *NRExternalTrace_enter(NRExternalTraceObject *self,
                                        PyObject *args)
{
    if (!self->transaction_trace) {
        Py_INCREF(self);
        return (PyObject *)self;
    }

    nr_node_header__record_starttime_and_push_current(
            (nr_node_header *)self->transaction_trace,
            &self->outer_transaction);

    Py_INCREF(self);
    return (PyObject *)self;
}

static PyObject *NRExternalTrace_exit(NRExternalTraceObject *self,
                                       PyObject *args)
{
    if (!self->transaction_trace) {
        Py_INCREF(Py_None);
        return Py_None;
    }

    nr_node_header__record_stoptime_and_pop_current(
            (nr_node_header *)self->transaction_trace,
            &self->outer_transaction);

    self->outer_transaction = NULL;

    Py_INCREF(Py_None);
    return Py_None;
}

static PyMethodDef NRExternalTrace_methods[] = {
    { "__enter__",  (PyCFunction)NRExternalTrace_enter,  METH_NOARGS, 0 },
    { "__exit__",   (PyCFunction)NRExternalTrace_exit,   METH_VARARGS, 0 },
    { NULL, NULL }
};

static PyGetSetDef NRExternalTrace_getset[] = {
    { NULL },
};

PyTypeObject NRExternalTrace_Type = {
    PyVarObject_HEAD_INIT(NULL, 0)
    "_newrelic.ExternalTrace", /*tp_name*/
    sizeof(NRExternalTraceObject), /*tp_basicsize*/
    0,                      /*tp_itemsize*/
    /* methods */
    (destructor)NRExternalTrace_dealloc, /*tp_dealloc*/
    0,                      /*tp_print*/
    0,                      /*tp_getattr*/
    0,                      /*tp_setattr*/
    0,                      /*tp_compare*/
    0,                      /*tp_repr*/
    0,                      /*tp_as_number*/
    0,                      /*tp_as_sequence*/
    0,                      /*tp_as_mapping*/
    0,                      /*tp_hash*/
    0,                      /*tp_call*/
    0,                      /*tp_str*/
    0,                      /*tp_getattro*/
    0,                      /*tp_setattro*/
    0,                      /*tp_as_buffer*/
    Py_TPFLAGS_DEFAULT,     /*tp_flags*/
    0,                      /*tp_doc*/
    0,                      /*tp_traverse*/
    0,                      /*tp_clear*/
    0,                      /*tp_richcompare*/
    0,                      /*tp_weaklistoffset*/
    0,                      /*tp_iter*/
    0,                      /*tp_iternext*/
    NRExternalTrace_methods, /*tp_methods*/
    0,                      /*tp_members*/
    NRExternalTrace_getset, /*tp_getset*/
    0,                      /*tp_base*/
    0,                      /*tp_dict*/
    0,                      /*tp_descr_get*/
    0,                      /*tp_descr_set*/
    0,                      /*tp_dictoffset*/
    0,                      /*tp_init*/
    0,                      /*tp_alloc*/
    0,                      /*tp_new*/
    0,                      /*tp_free*/
    0,                      /*tp_is_gc*/
};

/* ------------------------------------------------------------------------- */
