FUNCTION_NAME := kis-llm-trader-stock-digest
ZIP_FILE      := $(shell pwd)/../lambda.zip
PACKAGE_DIR   := $(shell pwd)/package

.PHONY: deploy clean

deploy: clean
	pip3 install -r requirements.txt \
		--platform manylinux2014_aarch64 \
		--target $(PACKAGE_DIR) \
		--python-version 3.12 \
		--only-binary=:all: \
		--quiet
	cp *.py $(PACKAGE_DIR)/
	cd $(PACKAGE_DIR) && zip -r $(ZIP_FILE) . --quiet
	aws lambda update-function-code \
		--function-name $(FUNCTION_NAME) \
		--zip-file fileb://$(ZIP_FILE) \
		--region ap-northeast-1
	rm -rf $(PACKAGE_DIR) $(ZIP_FILE)
	@echo "✅ 배포 완료"

clean:
	rm -rf $(PACKAGE_DIR) $(ZIP_FILE)
