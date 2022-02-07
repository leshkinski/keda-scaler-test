THIS_DIR = $(dir $(realpath $(firstword $(MAKEFILE_LIST))))
DEPLOY_DIR = $(THIS_DIR)/deploy
TEST_DIR = $(THIS_DIR)/test

init:
	cd $(DEPLOY_DIR); terraform init

plan: # init
	cd $(DEPLOY_DIR); terraform plan -var environment=$(ENV) -out terraform.tfplan

apply:
	cd $(DEPLOY_DIR); terraform apply terraform.tfplan

output:
	cd $(DEPLOY_DIR); terraform output

destroy:
	cd $(DEPLOY_DIR); terraform destroy -var environment=$(ENV)

requirements:
	pip install -r $(TEST_DIR)/requirements.txt

test: requirements
	cd $(TEST_DIR); ./keda/test_keda_scaler.py
