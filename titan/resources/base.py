from typing import ClassVar, Union, Dict, Set

from typing_extensions import Annotated

from inflection import underscore
from pydantic import BaseModel, Field, ConfigDict, BeforeValidator, field_validator, model_validator
from pydantic.functional_validators import AfterValidator
from pydantic._internal._model_construction import ModelMetaclass
from pyparsing import ParseException

from ..enums import Scope
from ..props import Props, IntProp, StringProp, TagsProp, FlagProp
from ..parse import _parse_create_header, _parse_props, _resolve_resource_class

# from ..sql import add_ref

from ..identifiers import FQN
from .validators import coerce_from_str


# TODO: snowflake resource name compatibility
# TODO: make this configurable
def normalize_resource_name(name: str):
    return name.upper()


ResourceName = Annotated[str, AfterValidator(normalize_resource_name)]


class _Resource(ModelMetaclass):
    classes = {}
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
        # Don't use this
        use_enum_values=True,
        # NOTE: This might be required to make SchemaScoped.schema = 'foo' work
        populate_by_name=True,
    )

    resource_type: ClassVar[str] = None
    props: ClassVar[Props]

    implicit: bool = Field(exclude=True, default=False)
    stub: bool = Field(exclude=True, default=False)
    _refs: Set["Resource"] = {}

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

    def _requires(self, resource):
        self._refs.add(resource)

    def requires(self, *resources):
        if isinstance(resources[0], list):
            resources = resources[0]
        for resource in resources:
            self._requires(resource)
        return self

    @property
    def references(self):
        return self._refs

    # def __format__(self, format_spec):
    #     add_ref(self)
    #     return self.fully_qualified_name


class Createable(BaseModel):
    def create_sql(self):
        return f"CREATE {self.resource_type} {self.name}"  # self.props.sql()

    # def create_or_replace_sql(self):
    #     return f"CREATE OR REPLACE {self.resource_type} {self.name}"


class ResourceChildren:
    def __init__(self, parent):
        self.parent = parent
        self.items = {}

    def _add(self, child):
        if child.resource_key not in self.items:
            self.items[child.resource_key] = []
        # TODO: dedupe?
        self.items[child.resource_key].append(child)
        child.parent = self.parent

    def add(self, *children):
        if isinstance(children[0], list):
            children = children[0]
        for child in children:
            self._add(child)

    def __contains__(self, child):
        return child.resource_key in self.items and child in self.items[child.resource_key]


class Organization(Resource):
    resource_type = "ORGANIZATION"

    name: ResourceName
    _children: ResourceChildren = Field(alias="children")

    def model_post_init(self, ctx):
        super().model_post_init(ctx)
        self._children = ResourceChildren(self)


class OrganizationScoped(BaseModel):
    scope: ClassVar[Scope] = Scope.ORGANIZATION

    organziation: Union[str, Organization] = Field(default=None, exclude=True)

    @property
    def parent(self):
        return self.organziation

    @parent.setter
    def parent(self, new_parent):
        if not isinstance(new_parent, Organization):
            raise ValueError(f"Parent must be an Organization, not {new_parent}")
        new_parent.children.add(self)

    @property
    def fully_qualified_name(self):
        return FQN(name=self.name.upper())

    @property
    def fqn(self):
        return self.fully_qualified_name


class Account(Resource, OrganizationScoped):
    resource_type = "ACCOUNT"

    name: ResourceName
    _children: ResourceChildren

    def model_post_init(self, ctx):
        super().model_post_init(ctx)
        self._children = ResourceChildren(self)

    @property
    def children(self):
        return self._children


class AccountScoped(BaseModel):
    scope: ClassVar[Scope] = Scope.ACCOUNT

    account: Annotated[Account, BeforeValidator(coerce_from_str(Account))] = Field(
        alias="parent", default=None, exclude=True
    )

    @property
    def parent(self):
        return self.account

    @parent.setter
    def parent(self, value):
        self.account = value

    @property
    def fully_qualified_name(self):
        return FQN(name=self.name.upper())

    @property
    def fqn(self):
        return self.fully_qualified_name


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

    _children: ResourceChildren = Field(alias="children")

    def model_post_init(self, ctx):
        super().model_post_init(ctx)
        self._children = ResourceChildren(self)
        self._children.add(
            Schema(name="PUBLIC", implicit=True),
            Schema(name="INFORMATION_SCHEMA", implicit=True),
        )

    @property
    def children(self):
        return self._children

    # @property
    # def schemas(self):
    #     return self._children.schemas


class DatabaseScoped(BaseModel):
    scope: ClassVar[Scope] = Scope.DATABASE
    database: Union[str, Database] = Field(default=None, exclude=True)

    @property
    def parent(self):
        return self.database

    @parent.setter
    def parent(self, value):
        self.database = value

    @property
    def fully_qualified_name(self):
        return FQN(database=self.database.name if self.database else None, name=self.name.upper())

    @property
    def fqn(self):
        return self.fully_qualified_name

    @field_validator("database")
    @classmethod
    def validate_database(cls, database: Union[str, Database]):
        return database if isinstance(database, Database) else Database(name=database, stub=True)


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

    _children: ResourceChildren = Field(alias="children")

    def model_post_init(self, ctx):
        super().model_post_init(ctx)
        self._children = ResourceChildren(self)

    @property
    def children(self):
        return self._children

    # @property
    # def tables(self):
    #     return self._children.tables


class SchemaScoped(BaseModel):
    scope: ClassVar[Scope] = Scope.SCHEMA
    schema_: Union[str, Schema] = Field(alias="schema", default=None, exclude=True)

    @property
    def parent(self):
        return self.schema_

    @parent.setter
    def parent(self, new_parent):
        if new_parent is None:
            return
        if not isinstance(new_parent, Schema):
            raise ValueError(f"Parent must be a Schema, not {new_parent}")
        # new_parent.children.add(self)
        self.schema_ = new_parent

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

    @field_validator("schema_")
    @classmethod
    def validate_schema(cls, schema: Union[str, Schema]):
        return schema if isinstance(schema, Schema) else Schema(name=schema, stub=True)
