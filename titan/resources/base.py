from typing import ClassVar, Dict, List, Type, Union, TYPE_CHECKING

from typing_extensions import Annotated

from inflection import underscore
from pydantic import BaseModel, Field, ConfigDict, BeforeValidator, PlainSerializer
from pydantic.functional_validators import AfterValidator
from pydantic._internal._model_construction import ModelMetaclass
from pyparsing import ParseException

from ..privs import DatabasePriv, GlobalPriv, Privs, SchemaPriv
from ..enums import AccountEdition, Scope
from ..props import BoolProp, EnumProp, Props, IntProp, StringProp, TagsProp, FlagProp
from ..parse import _parse_create_header, _parse_props, _resolve_resource_class
from ..sql import SQL, track_ref
from ..identifiers import FQN
from ..builder import tidy_sql
from .validators import coerce_from_str


# https://stackoverflow.com/questions/62884543/pydantic-autocompletion-in-vs-code
if TYPE_CHECKING:
    from dataclasses import dataclass as _fix_class_documentation
else:

    def _fix_class_documentation(cls):
        return cls


# TODO: snowflake resource name compatibility
# TODO: make this configurable
def normalize_resource_name(name: str):
    return name.upper()


# Consider making resource names immutable with Field(frozen=True)
ResourceName = Annotated[str, "str", AfterValidator(normalize_resource_name)]

serialize_resource_by_name = PlainSerializer(lambda resource: resource.name if resource else None, return_type=str)


class _Resource(ModelMetaclass):
    classes: Dict[str, Type["Resource"]] = {}
    resource_key: str = None

    def __new__(cls, name, bases, attrs):
        cls_ = super().__new__(cls, name, bases, attrs)
        cls_.resource_key = underscore(name)
        cls_.__doc__ = cls_.__doc__ or ""
        cls.classes[cls_.resource_key] = cls_
        return cls_


class Resource(BaseModel, metaclass=_Resource):
    model_config = ConfigDict(
        from_attributes=True,
        extra="forbid",
        validate_assignment=True,
        populate_by_name=True,
        # Don't use this
        use_enum_values=True,
    )

    lifecycle_privs: ClassVar[Privs] = None
    props: ClassVar[Props]
    resource_type: ClassVar[str] = None
    serialize_as_list: ClassVar[bool] = False

    implicit: bool = Field(exclude=True, default=False, repr=False)
    stub: bool = Field(exclude=True, default=False, repr=False)
    _refs: List["Resource"] = []

    def model_post_init(self, ctx):
        for field_name in self.model_fields.keys():
            field_value = getattr(self, field_name)
            if isinstance(field_value, Resource) and not field_value.stub:
                self._refs.append(field_value)
            elif isinstance(field_value, SQL):
                self._refs.extend(field_value.refs)
                setattr(self, field_name, field_value.sql)

    @classmethod
    def fetchable_fields(cls, data):
        data = data.copy()
        for key in list(data.keys()):
            field = cls.model_fields[key]
            fetchable = field.json_schema_extra is None or field.json_schema_extra.get("fetchable", True)
            if not fetchable:
                del data[key]
        return data

    @classmethod
    def from_sql(cls, sql):
        resource_cls = cls
        if resource_cls == Resource:
            resource_cls = Resource.classes[_resolve_resource_class(sql)]

        identifier, remainder_sql = _parse_create_header(sql, resource_cls)

        try:
            props = _parse_props(resource_cls.props, remainder_sql) if remainder_sql else {}
            return resource_cls(**identifier, **props)
        except ParseException as err:
            raise ParseException(f"Error parsing {resource_cls.__name__} props {identifier}") from err

    @property
    def refs(self):
        return self._refs

    def __format__(self, format_spec):
        track_ref(self)
        return self.fully_qualified_name

    def _requires(self, resource):
        self._refs.add(resource)

    def requires(self, *resources):
        if isinstance(resources[0], list):
            resources = resources[0]
        for resource in resources:
            self._requires(resource)
        return self

    @classmethod
    def lifecycle_create(cls, fqn, data, or_replace=False, if_not_exists=False):
        # TODO: modify props to split into header props and footer props
        return tidy_sql(
            "CREATE",
            "OR REPLACE" if or_replace else "",
            cls.resource_type,
            "IF NOT EXISTS" if if_not_exists else "",
            fqn,
            cls.props.render(data),
        )

    @classmethod
    def lifecycle_delete(cls, fqn, data, if_exists=False):
        return tidy_sql("DROP", cls.resource_type, "IF EXISTS" if if_exists else "", fqn)

    def create_sql(self, **kwargs):
        data = self.model_dump(exclude_none=True, exclude_defaults=True)
        return self.lifecycle_create(self.fqn, data, **kwargs)

    def drop_sql(self, **kwargs):
        data = self.model_dump(exclude_none=True, exclude_defaults=True)
        return self.lifecycle_delete(self.fqn, data, **kwargs)


@_fix_class_documentation
class Organization(Resource):
    resource_type = "ORGANIZATION"
    name: ResourceName


class OrganizationScoped(BaseModel):
    scope: ClassVar[Scope] = Scope.ORGANIZATION

    organization: Annotated[
        Organization,
        BeforeValidator(coerce_from_str(Organization)),
    ] = Field(default=None, exclude=True, repr=False)

    @property
    def fully_qualified_name(self):
        return FQN(name=self.name.upper())

    @property
    def fqn(self):
        return self.fully_qualified_name

    def has_scope(self):
        return self.organization is not None

    name: str


@_fix_class_documentation
class Account(Resource, OrganizationScoped):
    """
    CREATE ACCOUNT <name>
        ADMIN_NAME = <string>
        { ADMIN_PASSWORD = '<string_literal>' | ADMIN_RSA_PUBLIC_KEY = <string> }
        [ FIRST_NAME = <string> ]
        [ LAST_NAME = <string> ]
        EMAIL = '<string>'
        [ MUST_CHANGE_PASSWORD = { TRUE | FALSE } ]
        EDITION = { STANDARD | ENTERPRISE | BUSINESS_CRITICAL }
        [ REGION_GROUP = <region_group_id> ]
        [ REGION = <snowflake_region_id> ]
        [ COMMENT = '<string_literal>' ]
    """

    resource_type = "ACCOUNT"

    lifecycle_privs = Privs(
        create=GlobalPriv.CREATE_ACCOUNT,
    )

    props = Props(
        admin_name=StringProp("admin_name"),
        admin_password=StringProp("admin_password"),
        admin_rsa_public_key=StringProp("admin_rsa_public_key"),
        first_name=StringProp("first_name"),
        last_name=StringProp("last_name"),
        email=StringProp("email"),
        must_change_password=BoolProp("must_change_password"),
        edition=EnumProp("edition", AccountEdition),
        region_group=StringProp("region_group"),
        region=StringProp("region"),
        comment=StringProp("comment"),
    )

    name: ResourceName
    admin_name: str = Field(default=None, json_schema_extra={"fetchable": False})
    admin_password: str = Field(default=None, json_schema_extra={"fetchable": False})
    admin_rsa_public_key: str = Field(default=None, json_schema_extra={"fetchable": False})
    first_name: str = Field(default=None, json_schema_extra={"fetchable": False})
    last_name: str = Field(default=None, json_schema_extra={"fetchable": False})
    email: str = Field(default=None, json_schema_extra={"fetchable": False})
    must_change_password: bool = Field(default=None, json_schema_extra={"fetchable": False})
    # edition: AccountEdition = None
    # region_group: str = None
    # region: str = None
    comment: str = None

    @classmethod
    def lifecycle_create(cls, fqn, data):
        return tidy_sql(
            "CREATE ACCOUNT",
            fqn,
            cls.props.render(data),
        )

    @classmethod
    def lifecycle_delete(cls, fqn, data, if_exists=False, grace_period_in_days=3):
        return tidy_sql(
            "DROP ACCOUNT",
            "IF EXISTS" if if_exists else "",
            fqn,
            "GRACE_PERIOD_IN_DAYS = ",
            grace_period_in_days,
        )

    def add(self, *resources: "AccountScoped"):
        if isinstance(resources[0], list):
            resources = resources[0]
        for resource in resources:
            resource.account = self

    def remove(self, *resources: "AccountScoped"):
        if isinstance(resources[0], list):
            resources = resources[0]
        for resource in resources:
            resource.account = None


class AccountScoped(BaseModel):
    scope: ClassVar[Scope] = Scope.ACCOUNT

    account: Annotated[
        Account,
        BeforeValidator(coerce_from_str(Account)),
    ] = Field(default=None, exclude=True, repr=False)

    @property
    def fully_qualified_name(self):
        return FQN(name=self.name.upper())

    @property
    def fqn(self):
        return self.fully_qualified_name

    def has_scope(self):
        return self.account is not None


@_fix_class_documentation
class Database(Resource, AccountScoped):
    """
    CREATE [ OR REPLACE ] [ TRANSIENT ] DATABASE [ IF NOT EXISTS ] <name>
        [ CLONE <source_db>
                [ { AT | BEFORE } ( { TIMESTAMP => <timestamp> | OFFSET => <time_difference> | STATEMENT => <id> } ) ] ]
        [ DATA_RETENTION_TIME_IN_DAYS = <integer> ]
        [ MAX_DATA_EXTENSION_TIME_IN_DAYS = <integer> ]
        [ DEFAULT_DDL_COLLATION = '<collation_specification>' ]
        [ [ WITH ] TAG ( <tag_name> = '<tag_value>' [ , <tag_name> = '<tag_value>' , ... ] ) ]
        [ COMMENT = '<string_literal>' ]
    """

    resource_type = "DATABASE"

    lifecycle_privs = Privs(
        create=GlobalPriv.CREATE_DATABASE,
        read=DatabasePriv.USAGE,
        delete=DatabasePriv.OWNERSHIP,
    )

    props = Props(
        transient=FlagProp("transient"),
        data_retention_time_in_days=IntProp("data_retention_time_in_days"),
        max_data_extension_time_in_days=IntProp("max_data_extension_time_in_days"),
        default_ddl_collation=StringProp("default_ddl_collation"),
        tags=TagsProp(),
        comment=StringProp("comment"),
    )

    name: ResourceName
    transient: bool = False
    owner: str = "SYSADMIN"
    data_retention_time_in_days: int = 1
    max_data_extension_time_in_days: int = 14
    default_ddl_collation: str = None
    tags: Dict[str, str] = None
    comment: str = None

    def model_post_init(self, ctx):
        super().model_post_init(ctx)
        self.add(
            Schema(name="PUBLIC", implicit=True),
            Schema(name="INFORMATION_SCHEMA", implicit=True),
        )

    @classmethod
    def lifecycle_create(cls, fqn, data, or_replace=False, if_not_exists=False):
        return tidy_sql(
            "CREATE",
            "OR REPLACE" if or_replace else "",
            "TRANSIENT" if data.get("transient") else "",
            "DATABASE",
            "IF NOT EXISTS" if if_not_exists else "",
            fqn,
            cls.props.render(data),
        )

    @classmethod
    def lifecycle_update(cls, fqn, change, if_exists=False):
        attr, new_value = change.popitem()
        attr = attr.upper()
        if new_value is None:
            return tidy_sql(
                "ALTER DATABASE",
                "IF EXISTS" if if_exists else "",
                fqn,
                "UNSET",
                attr,
            )
        elif attr == "NAME":
            return tidy_sql(
                "ALTER DATABASE",
                "IF EXISTS" if if_exists else "",
                fqn,
                "RENAME TO",
                new_value,
            )
        else:
            new_value = f"'{new_value}'" if isinstance(new_value, str) else new_value
            return tidy_sql(
                "ALTER DATABASE",
                "IF EXISTS" if if_exists else "",
                fqn,
                "SET",
                attr,
                "=",
                new_value,
            )

    def add(self, *resources: "DatabaseScoped"):
        if isinstance(resources[0], list):
            resources = resources[0]
        for resource in resources:
            resource.database = self

    def remove(self, *resources: "DatabaseScoped"):
        if isinstance(resources[0], list):
            resources = resources[0]
        for resource in resources:
            resource.database = None


class DatabaseScoped(BaseModel):
    scope: ClassVar[Scope] = Scope.DATABASE

    database: Annotated[
        Database,
        BeforeValidator(coerce_from_str(Database)),
    ] = Field(default=None, exclude=True, repr=False)

    @property
    def fully_qualified_name(self):
        return FQN(database=self.database.name if self.database else None, name=self.name.upper())

    @property
    def fqn(self):
        return self.fully_qualified_name

    def has_scope(self):
        return self.database is not None


@_fix_class_documentation
class Schema(Resource, DatabaseScoped):
    """
    CREATE [ OR REPLACE ] [ TRANSIENT ] SCHEMA [ IF NOT EXISTS ] <name>
      [ CLONE <source_schema>
            [ { AT | BEFORE } ( { TIMESTAMP => <timestamp> | OFFSET => <time_difference> | STATEMENT => <id> } ) ] ]
      [ WITH MANAGED ACCESS ]
      [ DATA_RETENTION_TIME_IN_DAYS = <integer> ]
      [ MAX_DATA_EXTENSION_TIME_IN_DAYS = <integer> ]
      [ DEFAULT_DDL_COLLATION = '<collation_specification>' ]
      [ [ WITH ] TAG ( <tag_name> = '<tag_value>' [ , <tag_name> = '<tag_value>' , ... ] ) ]
      [ COMMENT = '<string_literal>' ]
    """

    resource_type = "SCHEMA"
    lifecycle_privs = Privs(
        create=DatabasePriv.CREATE_SCHEMA,
        read=SchemaPriv.USAGE,
        delete=SchemaPriv.OWNERSHIP,
    )
    props = Props(
        transient=FlagProp("transient"),
        with_managed_access=FlagProp("with managed access"),
        data_retention_time_in_days=IntProp("data_retention_time_in_days"),
        max_data_extension_time_in_days=IntProp("max_data_extension_time_in_days"),
        default_ddl_collation=StringProp("default_ddl_collation"),
        tags=TagsProp(),
        comment=StringProp("comment"),
    )

    name: ResourceName
    transient: bool = False
    owner: str = "SYSADMIN"
    with_managed_access: bool = None
    data_retention_time_in_days: int = None
    max_data_extension_time_in_days: int = None
    default_ddl_collation: str = None
    tags: Dict[str, str] = None
    comment: str = None

    def add(self, *resources: "SchemaScoped"):
        if isinstance(resources[0], list):
            resources = resources[0]
        for resource in resources:
            resource.schema = self

    def remove(self, *resources: "SchemaScoped"):
        if isinstance(resources[0], list):
            resources = resources[0]
        for resource in resources:
            resource.schema = None


T_Schema = Annotated[Schema, BeforeValidator(coerce_from_str(Schema)), serialize_resource_by_name]


class SchemaScoped(BaseModel):
    scope: ClassVar[Scope] = Scope.SCHEMA

    schema_: Annotated[
        Schema,
        BeforeValidator(coerce_from_str(Schema)),
    ] = Field(exclude=True, repr=False, alias="schema", default=None)

    @property
    def schema(self):
        return self.schema_

    @schema.setter
    def schema(self, new_schema):
        self.schema_ = new_schema

    @property
    def fully_qualified_name(self):
        schema = self.schema_.name if self.schema_ else None
        database = None
        if self.schema_ and self.schema_.database:
            database = self.schema_.database.name
        return FQN(database=database, schema=schema, name=self.name.upper())

    @property
    def fqn(self):
        return self.fully_qualified_name

    def has_scope(self):
        return self.schema_ is not None
