all: localecompile
LNGS:=`find eventyay_business/locale/ -mindepth 1 -maxdepth 1 -type d -printf "-l %f "`

localecompile:
	django-admin compilemessages

localegen:
	django-admin makemessages --add-location file --keep-pot -i build -i dist -i "*egg*" $(LNGS)

.PHONY: all localecompile localegen
