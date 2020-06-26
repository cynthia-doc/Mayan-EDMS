import hashlib

from django.contrib import contenttypes
from django.core.exceptions import ImproperlyConfigured
from django.db import models
from django.template.response import TemplateResponse
from django.urls import reverse
from django.utils.encoding import force_text
from django.utils.translation import ugettext_lazy as _

from mayan.apps.acls.classes import ModelPermission

from .links import link_object_copy
from .menus import menu_object
from .permissions import permission_object_copy
from .settings import setting_home_view


class ModelCopy:
    _registry = {}
    _lazy = {}

    @staticmethod
    def method_instance_copy(self, values=None):
        model_copy = ModelCopy.get(model=self._meta.model)
        model_copy.copy(instance=self, values=values)

    @classmethod
    def add_fields_lazy(cls, model, **kwargs):
        cls._lazy.setdefault(model, [])
        cls._lazy[model].append(kwargs)

    @classmethod
    def get(cls, model):
        return cls._registry[model]

    def __init__(self, model, bind_link=False, register_permission=False):
        self.fields = []
        self.fields_copy = []
        self.fields_foreign_keys = []
        self.fields_generic_related = []
        self.fields_many_to_many = []
        self.fields_reverse_related = []
        self.fields_many_to_many_reverse_related = []
        self.fields_related_one_to_one = []
        self.fields_unique = []

        self.model = model
        self.__class__._registry[model] = self
        model.add_to_class(
            name='copy_instance', value=ModelCopy.method_instance_copy
        )
        if bind_link:
            menu_object.bind_links(
                links=(link_object_copy,), sources=(model,), position=99
            )

        if register_permission:
            ModelPermission.register(
                model=model, permissions=(permission_object_copy,)
            )

        for entry in self.__class__._lazy.get(model, ()):
            self.add_fields(**entry)
            self.__class__._lazy.get(model).pop()

    def _evaluate_field_get_for_field(self, field, instance, value, values):
        field_value_gets = self.field_value_gets.get(field, None)
        if field_value_gets:
            related_model = self.model._meta.get_field(field).related_model or self.model._meta.get_field(field).model
            final_filter = {}
            context = {'instance': instance}
            context.update(values)
            for key, value in field_value_gets.items():
                final_filter[key] = (value.format(**context))

            value = related_model._meta.default_manager.get(**final_filter)

        return value

    def add_fields(
        self, field_names, excludes=None, field_value_gets=None,
        field_values=None, field_value_templates=None
    ):
        self.excludes = excludes or {}
        self.field_value_gets = field_value_gets or {}
        self.field_value_templates = field_value_templates or {}
        self.field_values = field_values or {}

        for field_name in field_names:
            self.fields.append(field_name)

            field = self.model._meta.get_field(field_name=field_name)

            if isinstance(field, models.fields.reverse_related.OneToOneRel):
                self.fields_related_one_to_one.append(field_name)
            elif isinstance(field, models.fields.reverse_related.ManyToOneRel):
                self.fields_reverse_related.append(field_name)
            elif isinstance(field, models.fields.related.ForeignKey):
                self.fields_foreign_keys.append(field_name)
            elif isinstance(field, contenttypes.fields.GenericRelation):
                self.fields_generic_related.append(field_name)
            elif isinstance(field, contenttypes.fields.GenericForeignKey):
                self.fields_foreign_keys.append(field_name)
            elif isinstance(field, models.fields.related.ManyToManyField):
                self.fields_many_to_many.append(field_name)
            elif isinstance(field, models.fields.reverse_related.ManyToManyRel):
                self.fields_many_to_many_reverse_related.append(field.related_name)
            else:
                if field.unique:
                    self.fields_unique.append(field_name)
                else:
                    self.fields_copy.append(field_name)

    def copy(self, instance, values=None):
        values = values or {}

        new_instance = self.model()

        if self.excludes:
            if self.model._meta.default_manager.filter(pk=instance.pk, **self.excludes).exists():
                return

        context = {'instance': instance}
        context.update(values)

        # Static values
        for field, value in self.field_values.items():
            setattr(new_instance, field, value)

        # Static values templates
        for field, value in self.field_value_templates.items():
            setattr(new_instance, field, value.format(**context))

        # Base fields whose values are copied
        for field in self.fields_copy:
            value = values.get(field, getattr(instance, field))

            value = self._evaluate_field_get_for_field(
                field=field, instance=instance, value=value, values=values
            )
            setattr(new_instance, field, value)

        # Base fields with unique values
        for field in self.fields_unique:
            base_value = getattr(instance, field)
            counter = 1

            while True:
                value = '{}_{}'.format(base_value, counter)
                if not self.model._meta.default_manager.filter(**{field: value}).exists():
                    break

                counter = counter + 1

            value = self._evaluate_field_get_for_field(
                field=field, instance=instance, value=value, values=values
            )

            setattr(new_instance, field, value)

        # Foreign keys
        for field in self.fields_foreign_keys:
            value = values.get(field, getattr(instance, field))

            value = self._evaluate_field_get_for_field(
                field=field, instance=instance, value=value, values=values
            )

            setattr(new_instance, field, value)

        new_instance.save()

        # Many to many fields added after instance creation
        for field in self.fields_many_to_many:
            getattr(new_instance, field).set(getattr(instance, field).all())

        # Many to many reverse related fields added after instance creation
        for field in self.fields_many_to_many_reverse_related:
            getattr(new_instance, field).set(getattr(instance, field).all())

        # Reverse related
        for field in self.fields_reverse_related:
            related_field = self.model._meta.get_field(field_name=field)
            related_field_name = related_field.field.name

            for related_instance in getattr(instance, field).all():
                related_instance.copy_instance(
                    values={related_field_name: new_instance}
                )

        # Reverse related one to one
        for field in self.fields_related_one_to_one:
            related_field = self.model._meta.get_field(field_name=field)
            related_field_name = related_field.field.name

            getattr(instance, field).copy_instance(
                values={related_field_name: new_instance}
            )

        # Generic relations
        for field in self.fields_generic_related:
            related_field = self.model._meta.get_field(field_name=field)
            related_field_name = 'content_object'

            for related_instance in getattr(instance, field).all():
                related_instance.copy_instance(
                    values={related_field_name: new_instance}
                )

    def get_fields_verbose_names(self):
        result = []

        for field_name in self.fields:
            field = self.model._meta.get_field(field_name=field_name)

            verbose_name = getattr(field, 'verbose_name', None)

            if not verbose_name and field.related_model:
                verbose_name = field.related_model._meta.verbose_name

            result.append(verbose_name)

        return result


class MissingItem:
    _registry = []

    @classmethod
    def get_all(cls):
        return cls._registry

    @classmethod
    def get_missing(cls):
        result = []
        for item in cls.get_all():
            if item.condition():
                result.append(item)
        return result

    def __init__(self, label, condition, description, view):
        self.label = label
        self.condition = condition
        self.description = description
        self.view = view
        self.__class__._registry.append(self)


class PropertyHelper:
    """
    Makes adding fields using __class__.add_to_class easier.
    Each subclass must implement the `constructor` and the `get_result`
    method.
    """
    @staticmethod
    @property
    def constructor(source_object):
        return PropertyHelper(source_object)

    def __init__(self, instance):
        self.instance = instance

    def __getattr__(self, name):
        return self.get_result(name=name)

    def get_result(self, name):
        """
        The method that produces the actual result. Must be implemented
        by each subclass.
        """
        raise NotImplementedError


class Template:
    _registry = {}

    @classmethod
    def all(cls, rendered=False, request=None):
        if not rendered:
            return cls._registry.values()
        else:
            result = []
            for template in cls._registry.values():
                result.append(template.render(request=request))
            return result

    @classmethod
    def get(cls, name):
        return cls._registry[name]

    def __init__(self, name, template_name):
        self.name = name
        self.template_name = template_name
        self.__class__._registry[name] = self

    def get_absolute_url(self):
        return reverse(
            kwargs={'name': self.name}, viewname='rest_api:template-detail'
        )

    def render(self, request):
        context = {
            'home_view': setting_home_view.value,
        }
        result = TemplateResponse(
            request=request,
            template=self.template_name,
            context=context,
        ).render()

        # Calculate the hash of the bytes version but return the unicode
        # version
        self.html = result.rendered_content.replace('\n', '')
        self.hex_hash = hashlib.sha256(result.content).hexdigest()
        return self


class ModelAttribute:
    _class_registry = []
    _model_registry = {}

    @classmethod
    def get_all_choices_for(cls, model):
        result = []

        for klass in cls._class_registry:
            klass_choices = klass.get_choices_for(model=model)
            if klass_choices:
                result.append(
                    (klass.class_label, klass_choices)
                )

        return result

    @classmethod
    def get_choices_for(cls, model):
        return sorted(
            [
                (entry.name, entry.get_display()) for entry in cls.get_for(model=model)
            ], key=lambda x: x[1]
        )

    @classmethod
    def get_for(cls, model):
        try:
            return cls._model_registry[cls.class_name][model]
        except KeyError:
            # We were passed a model instance, try again using the model of
            # the instance

            # If we are already in the model class, exit with an error
            if model.__class__ == models.base.ModelBase:
                return []

            return cls.get_for(model=type(model))

    @classmethod
    def register(cls, klass):
        cls._class_registry.append(klass)

    def __init__(self, model, name, label=None, description=None):
        self.model = model
        self.label = label
        self.name = name
        self.description = description
        self._model_registry.setdefault(self.class_name, {})
        self._model_registry[self.class_name].setdefault(model, [])
        self._model_registry[self.class_name][model].append(self)

    def get_display(self, show_name=False):
        if self.description:
            return '{} - {}'.format(
                self.name if show_name else self.label, self.description
            )
        else:
            return force_text(self.name if show_name else self.label)


class ModelProperty(ModelAttribute):
    class_label = _('Model properties')
    class_name = 'property'


class ModelField(ModelAttribute):
    class_label = _('Model fields')
    class_name = 'field'

    def __init__(self, *args, **kwargs):
        super(ModelField, self).__init__(*args, **kwargs)
        self._final_model_verbose_name = None

        if not self.label:
            self.label = self.get_field_attribute(
                attribute='verbose_name'
            )
            if self.label != self._final_model_verbose_name:
                self.label = '{}, {}'.format(
                    self._final_model_verbose_name, self.label
                )

        if not self.description:
            self.description = self.get_field_attribute(
                attribute='help_text'
            )

    def get_field_attribute(self, attribute, model=None, field_name=None):
        if not model:
            model = self.model

        if not field_name:
            field_name = self.name

        parts = field_name.split('__')
        if len(parts) > 1:
            return self.get_field_attribute(
                model=model._meta.get_field(parts[0]).related_model,
                field_name='__'.join(parts[1:]), attribute=attribute
            )
        else:
            self._final_model_verbose_name = model._meta.verbose_name
            return getattr(
                model._meta.get_field(field_name=field_name),
                attribute
            )


class ModelFieldRelated(ModelField):
    class_label = _('Model related fields')
    class_name = 'related_field'


class ModelReverseField(ModelField):
    class_label = _('Model reverse fields')
    class_name = 'reverse_field'

    def __init__(self, *args, **kwargs):
        super(ModelField, self).__init__(*args, **kwargs)
        self._final_model_verbose_name = None

        if not self.label:
            self.label = self.get_field_attribute(
                attribute='verbose_name_plural'
            )

    def get_field_attribute(self, attribute, model=None, field_name=None):
        if not model:
            model = self.model

        if not field_name:
            field_name = self.name

        return getattr(
            model._meta.get_field(field_name=field_name).related_model._meta,
            attribute
        )


class ModelQueryFields:
    _registry = {}

    @classmethod
    def get(cls, model):
        try:
            return cls._registry[model]
        except KeyError:
            ModelQueryFields(model=model)
            return cls.get(model=model)

    def __init__(self, model):
        self.model = model
        self.select_related_fields = []
        self.prefetch_related_fields = []
        self.__class__._registry[model] = self

    def add_select_related_field(self, field_name):
        if field_name in self.select_related_fields:
            raise ImproperlyConfigured(
                '"{}" model already has a "{}" query select related field.'.format(
                    self.model, field_name
                )
            )
        self.select_related_fields.append(field_name)

    def add_prefetch_related_field(self, field_name):
        if field_name in self.prefetch_related_fields:
            raise ImproperlyConfigured(
                '"{}" model already has a "{}" query prefetch related field.'.format(
                    self.model, field_name
                )
            )
        self.prefetch_related_fields.append(field_name)

    def get_queryset(self):
        queryset = self.model._meta.default_manager.all()

        if self.select_related_fields:
            queryset = queryset.select_related(*self.select_related_fields)

        if self.prefetch_related_fields:
            queryset = queryset.prefetch_related(*self.prefetch_related_fields)

        return queryset


ModelAttribute.register(klass=ModelProperty)
ModelAttribute.register(klass=ModelField)
ModelAttribute.register(klass=ModelFieldRelated)
ModelAttribute.register(klass=ModelReverseField)
