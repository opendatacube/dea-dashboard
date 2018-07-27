import json
import sys
import uuid
from datetime import datetime
from typing import Optional

import structlog
from geoalchemy2 import Geometry, WKBElement
from geoalchemy2.shape import to_shape
from psycopg2._range import Range as PgRange
from sqlalchemy import func, case, select, bindparam, Integer, SmallInteger, null
from sqlalchemy.dialects import postgresql as postgres
from sqlalchemy.engine import Engine

from cubedash._utils import alchemy_engine
from cubedash.summary._schema import DATASET_SPATIAL, SPATIAL_REF_SYS, PgGridCell
from cubedash.summary._summarise import GridCell
from datacube import Datacube
from datacube.drivers.postgres._fields import RangeDocField, PgDocField
from datacube.drivers.postgres._schema import DATASET
from datacube.index import Index
from datacube.model import MetadataType, DatasetType, Dataset

_LOG = structlog.get_logger()


def get_dataset_extent_alchemy_expression(md: MetadataType):
    """
    Build an SQLaLchemy expression to get the extent for a dataset.

    It's returned as a postgis geometry.

    The logic here mirrors the extent() function of datacube.model.Dataset.
    """
    doc = _jsonb_doc_expression(md)

    if 'grid_spatial' not in md.definition['dataset']:
        # Non-spatial product
        return None

    projection_offset = _projection_doc_offset(md)
    valid_data_offset = projection_offset + ['valid_data']

    return func.ST_SetSRID(
        case(
            [
                # If we have valid_data offset, use it as the polygon.
                (
                    doc[valid_data_offset] != None,
                    func.ST_GeomFromGeoJSON(
                        doc[valid_data_offset].astext,
                        type_=Geometry
                    )
                ),
            ],
            # Otherwise construct a polygon from the four corner points.
            else_=_bounds_polygon(doc, projection_offset),
        ),
        get_dataset_srid_alchemy_expression(md),
        type_=Geometry
    )


def _projection_doc_offset(md):
    projection_offset = md.definition['dataset']['grid_spatial']
    return projection_offset


def _jsonb_doc_expression(md):
    doc = md.dataset_fields['metadata_doc'].alchemy_expression
    return doc


def _bounds_polygon(doc, projection_offset):
    geo_ref_points_offset = projection_offset + ['geo_ref_points']
    return func.ST_MakePolygon(
        func.ST_MakeLine(
            postgres.array(tuple(
                _gis_point(doc, geo_ref_points_offset + [key])
                for key in ('ll', 'ul', 'ur', 'lr', 'll')
            ))
        ), type_=Geometry
    )


def _grid_point_fields(dt: DatasetType):
    """
    Get an sqlalchemy expression to calculte the grid number of a dataset.

    Eg.
        On scenes this is the path/row
        On tiles this is the tile numbers

    Returns as a postgres array of small int.
    """
    grid_spec = dt.grid_spec

    md_fields = dt.metadata_type.dataset_fields

    # If the product has a grid spec, we can calculate the grid number
    if grid_spec is not None:
        doc = _jsonb_doc_expression(dt.metadata_type)
        projection_offset = _projection_doc_offset(dt.metadata_type)

        # Calculate tile refs

        geo_ref_points_offset = projection_offset + ['geo_ref_points']
        center_point = func.ST_Centroid(func.ST_Collect(
            _gis_point(doc, geo_ref_points_offset + ['ll']),
            _gis_point(doc, geo_ref_points_offset + ['ur']),
        ))

        # todo: look at grid_spec crs. Use it for defaults, conversion.
        size_x, size_y = (grid_spec.tile_size or (1000.0, 1000.0))
        origin_x, origin_y = grid_spec.origin
        return func.ROW(
            func.floor((func.ST_X(center_point) - origin_x) / size_x).cast(
                SmallInteger),
            func.floor((func.ST_Y(center_point) - origin_y) / size_y).cast(
                SmallInteger),
        ).cast(PgGridCell)
    # Otherwise does the product have a "sat_path/sat_row" fields? Use their values directly.
    elif 'sat_path' in md_fields:
        # Use sat_path/sat_row as grid items
        path_field: RangeDocField = md_fields['sat_path']
        row_field: RangeDocField = md_fields['sat_row']

        return func.ROW(
            path_field.lower.alchemy_expression.cast(SmallInteger),
            row_field.greater.alchemy_expression.cast(SmallInteger),
        ).cast(PgGridCell)
    else:
        _LOG.warn(
            "no_grid_spec",
            product_name=dt.name,
            metadata_type_name=dt.metadata_type.name
        )
        return null()


def get_dataset_srid_alchemy_expression(md: MetadataType):
    doc = md.dataset_fields['metadata_doc'].alchemy_expression

    if 'grid_spatial' not in md.definition['dataset']:
        # Non-spatial product
        return None

    projection_offset = md.definition['dataset']['grid_spatial']

    # Most have a spatial_reference field we can use directly.
    spatial_reference_offset = projection_offset + ['spatial_reference']
    spatial_ref = doc[spatial_reference_offset].astext
    return func.coalesce(
        case(
            [
                (
                    # If matches shorthand code: eg. "epsg:1234"
                    spatial_ref.op("~")(r"^[A-Za-z0-9]+:[0-9]+$"),
                    select([SPATIAL_REF_SYS.c.srid]).where(
                        func.lower(SPATIAL_REF_SYS.c.auth_name) ==
                        func.lower(func.split_part(spatial_ref, ':', 1))
                    ).where(
                        SPATIAL_REF_SYS.c.auth_srid ==
                        func.split_part(spatial_ref, ':', 2).cast(Integer)
                    ).as_scalar()
                )
            ],
            else_=None
        ),
        # Some older datasets have datum/zone fields instead.
        # The only remaining ones in DEA are 'GDA94'.
        case(
            [
                (
                    doc[(projection_offset + ['datum'])].astext == 'GDA94',
                    select([SPATIAL_REF_SYS.c.srid]).where(
                        SPATIAL_REF_SYS.c.auth_name == 'EPSG'
                    ).where(
                        SPATIAL_REF_SYS.c.auth_srid == (
                            '283' + func.abs(
                                doc[(projection_offset + ['zone'])].astext.cast(Integer)
                            )
                        ).cast(Integer)
                    ).as_scalar()
                )
            ],
            else_=None
        )
        # TODO: third option: CRS as text/WKT
    )


def _gis_point(doc, doc_offset):
    return func.ST_MakePoint(
        doc[doc_offset + ['x']].astext.cast(postgres.DOUBLE_PRECISION),
        doc[doc_offset + ['y']].astext.cast(postgres.DOUBLE_PRECISION)
    )


def refresh_product(index: Index, product: DatasetType):
    engine: Engine = alchemy_engine(index)
    insert_count = _populate_missing_dataset_extents(engine, product)
    return insert_count


def _populate_missing_dataset_extents(engine: Engine, product: DatasetType):
    query = postgres.insert(DATASET_SPATIAL).from_select(
        ['id', 'dataset_type_ref', 'center_time', 'footprint', 'grid_point', 'creation_time'],
        _select_dataset_extent_query(product)
    ).on_conflict_do_nothing(
        index_elements=['id']
    )

    _LOG.debug(
        'spatial_insert_query.start',
        product_name=product.name,
        query_sql=as_sql(query),
    )
    inserted = engine.execute(query).rowcount
    _LOG.debug(
        'spatial_insert_query.end',
        product_name=product.name,
        inserted=inserted
    )
    return inserted


def _select_dataset_extent_query(dt: DatasetType):
    md_type = dt.metadata_type
    # If this product has lat/lon fields, we can take spatial bounds.

    footrprint_expression = get_dataset_extent_alchemy_expression(md_type)
    product_ref = bindparam('product_ref', dt.id, type_=SmallInteger)

    # "expr == None" is valid in sqlalchemy:
    # pylint: disable=singleton-comparison
    time = md_type.dataset_fields['time'].alchemy_expression
    return select([
        DATASET.c.id,
        DATASET.c.dataset_type_ref,
        (
            func.lower(time) + (func.upper(time) - func.lower(time)) / 2
        ).label('center_time'),
        (
            null() if footrprint_expression is None else footrprint_expression
        ).label('footprint'),
        _grid_point_fields(dt).label('grid_point'),
        _dataset_creation_expression(md_type).label('creation_time'),
    ]).where(
        DATASET.c.dataset_type_ref == product_ref
    ).where(
        DATASET.c.archived == None
    )


def _dataset_creation_expression(md: MetadataType) -> Optional[datetime]:
    """SQLAlchemy expression for the creation (processing) time of a dataset"""

    # Either there's a field called "created", or we fallback to the default "creation_dt' in metadata type.
    created_field = md.dataset_fields.get('created')
    if created_field is not None:
        assert isinstance(created_field, PgDocField)
        return created_field.alchemy_expression

    doc = md.dataset_fields['metadata_doc'].alchemy_expression
    creation_dt = md.definition['dataset'].get('creation_dt') or ['creation_dt']
    return func.agdc.common_timestamp(doc[creation_dt].astext)


def get_dataset_bounds_query(md_type):
    if 'lat' not in md_type.dataset_fields:
        # Not a spatial product
        return None

    lat, lon = md_type.dataset_fields['lat'], md_type.dataset_fields['lon']
    assert isinstance(lat, RangeDocField)
    assert isinstance(lon, RangeDocField)
    return func.ST_MakeBox2D(
        func.ST_MakePoint(lat.lower.alchemy_expression,
                          lon.lower.alchemy_expression),
        func.ST_MakePoint(lat.greater.alchemy_expression,
                          lon.greater.alchemy_expression),
        type_=Geometry
    )


def as_sql(expression, **params):
    """Convert sqlalchemy expression to SQL string.

    (primarily for debugging: to see what sqlalchemy is doing)

    This has its literal values bound, so it's more readable than the engine's
    query logging.
    """
    if params:
        expression = expression.params(**params)
    return str(expression.compile(
        dialect=postgres.dialect(),
        compile_kwargs={"literal_binds": True}
    ))


def _as_json(obj):
    def fallback(o, *args, **kwargs):
        if isinstance(o, uuid.UUID):
            return str(o)
        if isinstance(o, WKBElement):
            # Following the EWKT format: include srid
            prefix = f'SRID={o.srid};' if o.srid else ''
            return prefix + to_shape(o).wkt
        if isinstance(o, GridCell):
            return [o.x, o.y]
        if isinstance(o, datetime):
            return o.isoformat()
        if isinstance(o, PgRange):
            return ['∞' if o.lower_inf else o.lower,
                    '∞' if o.upper_inf else o.upper]
        return repr(o)

    return json.dumps(obj, indent=4, default=fallback)


def print_sample_dataset(*product_names: str):
    with Datacube(env='clone') as dc:
        index = dc.index
        for product_name in product_names:
            product = index.products.get_by_name(product_name)
            res = alchemy_engine(index).execute(
                _select_dataset_extent_query(product).limit(1)
            ).fetchone()
            print(_as_json(dict(res)))


if __name__ == '__main__':
    print_sample_dataset(
        *(sys.argv[1:] or ['ls8_nbar_scene', 'ls8_nbar_albers'])
    )