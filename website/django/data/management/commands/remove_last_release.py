from django.core.management.base import BaseCommand, CommandError
from django.db import connection
from data.models import Variant, DataRelease
from django.db import transaction


class Command(BaseCommand):
    help = 'Remove the most recent release from the database'

    def update_autocomplete_words(self):
        # Drop words table and recreate with latest data
        with connection.cursor() as cursor:
            cursor.execute("""
                DROP TABLE IF EXISTS words;
                CREATE TABLE words AS SELECT DISTINCT left(word, 300) as word, release_id FROM (
                SELECT regexp_split_to_table(lower("Genomic_Coordinate_hg38"), '[\s|''"]') as word, "Data_Release_id" as release_id from variant UNION
                SELECT regexp_split_to_table(lower("Genomic_Coordinate_hg37"), '[\s|''"]') as word, "Data_Release_id" as release_id from variant UNION
                SELECT regexp_split_to_table(lower("Genomic_Coordinate_hg36"), '[\s|''"]') as word, "Data_Release_id" as release_id from variant UNION
                SELECT regexp_split_to_table(lower("Clinical_significance_ENIGMA"), '[\s|''"]') as word, "Data_Release_id" as release_id from variant UNION
                SELECT regexp_split_to_table(lower("Gene_Symbol"), '[\s|''"]') as word, "Data_Release_id" as release_id from variant UNION
                SELECT regexp_split_to_table(lower("Reference_Sequence"), '[\s|''"]') as word, "Data_Release_id" as release_id from variant UNION
                SELECT regexp_split_to_table(lower("HGVS_cDNA"), '[\s|:''"]')  as word, "Data_Release_id" as release_id from variant UNION
                SELECT regexp_split_to_table(lower("BIC_Nomenclature"), '[\s|''"]') as word, "Data_Release_id" as release_id from variant UNION
                SELECT regexp_split_to_table(lower("HGVS_Protein"), '[\s|''"]') as word, "Data_Release_id" as release_id from variant
                )
                AS combined_words;

                CREATE INDEX words_idx ON words(word text_pattern_ops);
            """)

    @transaction.atomic
    def handle(self, *args, **options):
        latest_release_id = DataRelease.objects.all().order_by("-id")[0].id

        # Delete variants from latest release
        Variant.objects.filter(Data_Release_id=latest_release_id).delete()

        print "Deleted variants from most recent release."

        # Delete latest data_release and update materialized view of variants
        with connection.cursor() as cursor:
            cursor.execute("DELETE FROM data_release WHERE id = %s", [latest_release_id])
            cursor.execute("REFRESH MATERIALIZED VIEW currentvariant")

        print "Deleted most recent data_release and updated materialized view."

        self.update_autocomplete_words()

        print "Updated autocomplete words."
        print "Done!"