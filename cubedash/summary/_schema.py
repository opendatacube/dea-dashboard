from __future__ import absolute_import

import re

from geoalchemy2 import Geometry
from psycopg2.extensions import adapt, AsIs
from sqlalchemy import DateTime, Date
from sqlalchemy import Enum, JSON, event, DDL, \
    CheckConstraint
from sqlalchemy import func, Table, Column, ForeignKey, String, \
    Integer, SmallInteger, MetaData, Index
from sqlalchemy.dialects import postgresql as postgres
from sqlalchemy.dialects.postgresql import TSTZRANGE
from sqlalchemy.types import UserDefinedType

from datacube.drivers.postgres._schema import DATASET_TYPE
from ._summarise import GridCell

CUBEDASH_SCHEMA = 'cubedash'
METADATA = MetaData(schema=CUBEDASH_SCHEMA)


class PgGridCell(UserDefinedType):
    """
    A composite type with smallint x/y

    For landsat path row and tile ids.
    """
    def get_col_spec(self):
        return f'{CUBEDASH_SCHEMA}.gridcell'

    def bind_processor(self, dialect):
        def process(gridcell):
            if gridcell is None:
                return None
            x = adapt(gridcell.x).getquoted()
            y = adapt(gridcell.y).getquoted()
            return AsIs("'(%s, %s)'" % (x, y))

        return process

    def result_processor(self, dialect, coltype):
        def process(value):
            m = _PG_GRIDCELL_STRING.match(value)
            if m:
                return GridCell(int(m.group(1)), int(m.group(2)))
            else:
                raise ValueError("bad grid_cell representation: %r" % value)

        return process

    @property
    def python_type(self):
        return GridCell


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
    Column(
        'id',
        postgres.UUID(as_uuid=True),
        primary_key=True,
        comment='Dataset ID',
    ),
    # Note that we deliberately don't foreign-key to datacube tables:
    # - We don't want to add an external dependency on datacube core (breaking, eg, product deletion scripts)
    # - they may be in a separate database.
    Column(
        'dataset_type_ref',
        SmallInteger,
        comment='Cubedash product list '
                '(corresponding to datacube dataset_type)',
        nullable=False,
    ),
    Column('center_time', DateTime(timezone=True)),
    Column('footprint', Geometry(spatial_index=False), ),
    Column('grid_point', PgGridCell),
    # When was the dataset created? creation_time if it has one, otherwise datacube index time.
    Column('creation_time', DateTime(timezone=True), nullable=False),

    Index('product_ref', 'center_time'),
)

# Note that we deliberately don't foreign-key to datacube tables:
# - We don't want to add an external dependency on datacube core (breaking, eg, product deletion scripts)
# - they may be in a separate database.
PRODUCT = Table(
    'product', METADATA,
    Column('id', SmallInteger, primary_key=True),
    Column('name', String, unique=True, nullable=False),

    Column('time_earliest', DateTime(timezone=True)),
    Column('time_latest', DateTime(timezone=True)),
)

TIME_OVERVIEW = Table(
    'time_overview', METADATA,
    # Uniquely identified by three values:
    Column('product_ref', None, ForeignKey(PRODUCT.c.id), primary_key=True),
    Column('start_day', Date, primary_key=True),
    Column('period_type', Enum('all', 'year', 'month', 'day', name='overviewperiod'),
           primary_key=True),

    Column('dataset_count', Integer, nullable=False),

    Column('timeline_dataset_start_days', postgres.ARRAY(DateTime(timezone=True))),
    Column('timeline_dataset_counts', postgres.ARRAY(Integer)),

    # Only when there's a small number of them.
    # GeoJSON featurecolleciton as it contains metadata per dataset (the id etc).
    Column('datasets_geojson', JSON, nullable=True),

    Column('timeline_period',
           Enum('year', 'month', 'week', 'day', name='timelineperiod')),

    # Frustrating that there's no default datetimetz range type by default in postgres
    Column('time_earliest', DateTime(timezone=True)),
    Column('time_latest', DateTime(timezone=True)),

    Column('footprint_geometry', Geometry()),

    Column('footprint_count', Integer),

    Column('crses', postgres.ARRAY(String)),

    # The most newly created dataset
    Column('newest_dataset_creation_time', DateTime(timezone=True)),

    # When this summary was generated
    Column(
        'generation_time',
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False
    ),

    CheckConstraint(
        r"array_length(timeline_dataset_start_days, 1) = "
        r"array_length(timeline_dataset_counts, 1)",
        name='timeline_lengths_equal'
    ),
)

_PG_GRIDCELL_STRING = re.compile(r"\(([^)]+),([^)]+)\)")




event.listen(
    METADATA,
    'before_create',
    DDL(f"create schema if not exists {CUBEDASH_SCHEMA}")
)
event.listen(
    METADATA,
    'before_create',
    DDL(f"create extension if not exists postgis")
)


@event.listens_for(METADATA, 'before_create')
def create(target, connection, **kw):
    """
    Create all tables if the cubedash schema doesn't already exist.
    """
    if not pg_exists(connection, f"{CUBEDASH_SCHEMA}.gridcell"):
        connection.execute(f"create type {CUBEDASH_SCHEMA}.gridcell "
                           f"as (x smallint, y smallint);")

    # Ensure there's an index on the SRS table. (Using default pg naming conventions)
    # (Postgis doesn't add one by default, but we're going to do a lot of lookups)
    connection.execute("""
        create index if not exists
            spatial_ref_sys_auth_name_auth_srid_idx
        on spatial_ref_sys(auth_name, auth_srid);
    """)


def pg_exists(conn, name):
    """
    Does a postgres object exist?
    :rtype bool
    """
    return conn.execute("SELECT to_regclass(%s)", name).scalar() is not None


def _add_convenience_views(engine):
    """
    Convenience view for seeing product names, sizes and readable geometry
    """
    engine.execute("""
    create or replace view cubedash.view_extents as 
    select 
      dt.name as product, 
      ST_AsEWKT(sizes.footprint) as footprint_ewkt, 
      sizes.* 
    from cubedash.dataset_spatial sizes 
    inner join agdc.dataset_type dt on sizes.dataset_type_ref = dt.id;
    """)

    engine.execute("""
    create or replace view cubedash.view_space_usage as (
        select 
            dt.name as product, 
            sizes.* 
        from (
            select
            dataset_type_ref,
            count(*),
            pg_size_pretty(sum(pg_column_size(id               ))) as id_col_size,
            pg_size_pretty(sum(pg_column_size(time             ))) as time_col_size,
            pg_size_pretty(sum(pg_column_size(footprint))) as footprint_col_size,
            pg_size_pretty(round(avg(pg_column_size(footprint)), 0)) as avg_footprint_col_size,
            count(*) filter (where (not ST_IsValid(footprint))) as invalid_footprints,
            count(*) filter (where (ST_SRID(footprint) is null)) as missing_srid
            from cubedash.dataset_spatial
            group by 1
        ) sizes
        inner join agdc.dataset_type dt on sizes.dataset_type_ref = dt.id
        order by sizes.count desc
    );
    """)

    engine.execute("""
    create materialized view if not exists cubedash.product_extent as (
        select dataset_type_ref, 
               tstzrange(min(lower(time)), max(upper(time))) as time, 
               ST_Extent(footprint) as footprint,
               array_agg(distinct grid_point) as points
        from cubedash.dataset_spatial 
        group by 1
    );
    """)