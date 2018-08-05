from __future__ import absolute_import

import re

import psycopg2
from geoalchemy2 import Geometry
from psycopg2._psycopg import AsIs
from psycopg2.extensions import register_adapter
from psycopg2.extras import register_composite
from sqlalchemy import DateTime, Date, BigInteger, PrimaryKeyConstraint
from sqlalchemy import Enum, DDL, CheckConstraint
from sqlalchemy import func, Table, Column, ForeignKey, String, Integer, SmallInteger, MetaData, Index
from sqlalchemy.dialects import postgresql as postgres
from sqlalchemy.engine import Engine
from sqlalchemy.types import UserDefinedType

from ._model import GridCell

CUBEDASH_SCHEMA = 'cubedash'
METADATA = MetaData(schema=CUBEDASH_SCHEMA)
GRIDCELL_COL_SPEC = f'{CUBEDASH_SCHEMA}.gridcell'


class PgGridCell(UserDefinedType):
    """
    A composite type with smallint x/y

    For landsat path row and tile ids.
    """

    def get_col_spec(self):
        return GRIDCELL_COL_SPEC

    @property
    def python_type(self):
        return GridCell


def adapt_point(point):
    return AsIs("'(%s, %s)'::%s" % (point.x, point.y, GRIDCELL_COL_SPEC))


register_adapter(GridCell, adapt_point)

POSTGIS_METADATA = MetaData(schema='public')
SPATIAL_REF_SYS = Table(
    'spatial_ref_sys', POSTGIS_METADATA,
    Column('srid', Integer, primary_key=True),
    Column('auth_name', String(255)),
    Column('auth_srid', Integer),
    Column('srtext', String(2048)),
    Column('proj4text', String(2048)),
)

DATASET_SPATIAL = Table(
    'dataset_spatial',
    METADATA,
    # Note that we deliberately don't foreign-key to datacube tables:
    # - We don't want to add an external dependency on datacube core (breaking, eg, product deletion scripts)
    # - they may be in a separate database.
    Column(
        'id',
        postgres.UUID(as_uuid=True),
        primary_key=True,
        comment='Dataset ID',
    ),
    Column(
        'dataset_type_ref',
        SmallInteger,
        comment='Cubedash product list '
                '(corresponding to datacube dataset_type)',
        nullable=False,
    ),
    Column('center_time', DateTime(timezone=True), nullable=False),

    # When was the dataset created? creation_time if it has one, otherwise datacube index time.
    Column('creation_time', DateTime(timezone=True), nullable=False),

    # Must be nullable as currently satellite_telemetry products have no path/row field in their md type.
    Column('grid_point', PgGridCell),
    # Size of this dataset in bytes, if the product includes it.
    Column('size_bytes', BigInteger),

    Column('footprint', Geometry(spatial_index=False)),

    # Default postgres naming conventions.
    Index("dataset_spatial_dataset_type_ref_center_time_idx", 'dataset_type_ref', 'center_time'),
)

# Note that we deliberately don't foreign-key to datacube tables:
# - We don't want to add an external dependency on datacube core (breaking, eg, product deletion scripts)
# - they may be in a separate database.
PRODUCT = Table(
    'product', METADATA,
    Column('id', SmallInteger, primary_key=True),
    Column('name', String, unique=True, nullable=False),

    Column('dataset_count', Integer, nullable=False),

    Column(
        'last_refresh',
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        comment="Last refresh of this product in the dataset_spatial table",
    ),

    Column('time_earliest', DateTime(timezone=True)),
    Column('time_latest', DateTime(timezone=True)),
)
TIME_OVERVIEW = Table(
    'time_overview', METADATA,
    # Uniquely identified by three values:
    Column('product_ref', None, ForeignKey(PRODUCT.c.id)),
    Column('period_type', Enum('all', 'year', 'month', 'day', name='overviewperiod')),
    Column('start_day', Date),

    Column('dataset_count', Integer, nullable=False),

    # Time range (if there's at least one dataset)
    Column('time_earliest', DateTime(timezone=True)),
    Column('time_latest', DateTime(timezone=True)),

    Column('timeline_period',
           Enum('year', 'month', 'week', 'day', name='timelineperiod'),
           nullable=False),

    Column(
        'timeline_dataset_start_days',
        postgres.ARRAY(DateTime(timezone=True)),
        nullable=False
    ),
    Column(
        'timeline_dataset_counts',
        postgres.ARRAY(Integer),
        nullable=False
    ),

    Column('grid_dataset_grids', postgres.ARRAY(PgGridCell), nullable=False),
    Column('grid_dataset_counts', postgres.ARRAY(Integer), nullable=False),

    # The most newly created dataset
    Column(
        'newest_dataset_creation_time',
        DateTime(timezone=True)
    ),

    # When this summary was generated
    Column(
        'generation_time',
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False
    ),

    Column('footprint_count', Integer, nullable=False),
    Column('footprint_geometry', Geometry()),
    Column('crses', postgres.ARRAY(String)),

    # Size of this dataset in bytes, if the product includes it.
    Column('size_bytes', BigInteger),

    PrimaryKeyConstraint('product_ref', 'start_day', 'period_type'),
    CheckConstraint(
        r"array_length(timeline_dataset_start_days, 1) = "
        r"array_length(timeline_dataset_counts, 1)",
        name='timeline_lengths_equal'
    ),
)

_PG_GRIDCELL_STRING = re.compile(r"\(([^)]+),([^)]+)\)")


def create_schema(engine: Engine):
    engine.execute(DDL(f"create extension if not exists postgis"))
    engine.execute(DDL(f"create schema if not exists {CUBEDASH_SCHEMA}"))

    if not pg_exists(engine, f"{CUBEDASH_SCHEMA}.gridcell"):
        engine.execute(f"create type {CUBEDASH_SCHEMA}.gridcell "
                       f"as (x smallint, y smallint);")

    load_types(engine)

    # Ensure there's an index on the SRS table. (Using default pg naming conventions)
    # (Postgis doesn't add one by default, but we're going to do a lot of lookups)
    engine.execute("""
        create index if not exists
            spatial_ref_sys_auth_name_auth_srid_idx
        on spatial_ref_sys(auth_name, auth_srid);
    """)
    METADATA.create_all(engine, checkfirst=True)


def load_types(engine: Engine):
    register_composite('cubedash.gridcell', engine.raw_connection(), globally=True, factory=GridCellComposite)


class GridCellComposite(psycopg2.extras.CompositeCaster):
    def make(self, values):
        return GridCell(*values)


def pg_exists(conn, name):
    """
    Does a postgres object exist?
    :rtype bool
    """
    return conn.execute("SELECT to_regclass(%s)", name).scalar() is not None
