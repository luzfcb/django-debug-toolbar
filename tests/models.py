from django.conf import settings
from django.db import models
from django.db.models import JSONField
from django.db.models.functions import Lower


class NonAsciiRepr:
    def __repr__(self):
        return "nôt åscíì"


class Binary(models.Model):
    field = models.BinaryField()

    def __str__(self):
        return ""


class PostgresJSON(models.Model):
    field = JSONField()

    def __str__(self):
        return ""


class OraclePlanAuthor(models.Model):
    name = models.CharField(max_length=64)
    region = models.CharField(max_length=16, db_index=True)

    class Meta:
        indexes = [
            models.Index(fields=["region", "name"], name="dt_opa_region_name"),
        ]

    def __str__(self):
        return ""


class OraclePlanBook(models.Model):
    author = models.ForeignKey(OraclePlanAuthor, on_delete=models.CASCADE)
    code = models.CharField(max_length=32, unique=True)
    category = models.CharField(max_length=16, db_index=True)
    rating = models.IntegerField(db_index=True)
    title = models.CharField(max_length=128)
    notes = models.TextField(blank=True)
    published = models.BooleanField(default=True)

    class Meta:
        indexes = [
            models.Index(fields=["category", "rating"], name="dt_opb_cat_rating"),
            models.Index(fields=["category", "-rating"], name="dt_opb_cat_rat_desc"),
            models.Index(fields=["published", "category"], name="dt_opb_pub_cat"),
            models.Index(Lower("title"), name="dt_opb_lower_title"),
        ]

    def __str__(self):
        return ""


if settings.USE_GIS:
    from django.contrib.gis.db import models as gismodels

    class Location(gismodels.Model):
        point = gismodels.PointField()

        def __str__(self):
            return ""
