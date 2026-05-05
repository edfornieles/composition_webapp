from django.db import migrations, models
import django.db.models.deletion
import django.utils.timezone


class Migration(migrations.Migration):

    dependencies = [
        ("djangoscrap", "0052_add_media_asset_archive"),
    ]

    operations = [
        migrations.CreateModel(
            name="NFTMintVoucher",
            fields=[
                ("id", models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                (
                    "composition",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="mint_vouchers",
                        to="djangoscrap.composition",
                    ),
                ),
                ("min_price_wei", models.BigIntegerField(help_text="Minimum ETH in wei (e.g. 70000000000000000 = 0.07 ETH)")),
                ("uri", models.CharField(max_length=500, help_text="Metadata URI embedded in the voucher (e.g. ipfs://...)")),
                ("nonce", models.BigIntegerField(unique=True, help_text="Unique nonce to prevent replay")),
                ("signature", models.CharField(max_length=200, help_text="0x-prefixed EIP-712 signature from authorizedSigner")),
                ("signed_by", models.CharField(max_length=42, help_text="Ethereum address that signed this voucher")),
                ("signed_at", models.DateTimeField(blank=True, null=True)),
                ("redeemed", models.BooleanField(default=False)),
                ("redeemed_tx", models.CharField(blank=True, default="", max_length=128, help_text="Transaction hash when redeemed")),
                ("redeemed_token_id", models.CharField(blank=True, default="", max_length=64)),
                ("redeemed_by", models.CharField(blank=True, default="", max_length=42, help_text="Collector wallet address")),
                ("redeemed_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(default=django.utils.timezone.now)),
            ],
            options={
                "ordering": ["-created_at"],
            },
        ),
    ]
