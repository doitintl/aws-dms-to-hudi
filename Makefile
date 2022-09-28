check_defined = \
    $(strip $(foreach 1,$1, \
        $(call __check_defined,$1,$(strip $(value 2)))))
__check_defined = \
    $(if $(value $1),, \
      $(error Undefined $1$(if $2, ($2))))

build: clean mkdirs do_build

clean:
	rm -rf build ;

mkdirs:
	mkdir -p build/lambdas ;

do_build:
	cp templates/aws-dms-to-hudi.yaml build/
	cp -r src/stepfunctions/ build/stepfunctions/
	cp -r src/lambdas/ build/lambdas/ ; \
	cd build/lambdas ; \
	for i in `ls -1`; do \
		cd $$i; ls; \
		[ -f requirements.txt ] && pip install -r requirements.txt -t . || echo "$$i no requirements.txt"; \
		zip -r ../$$i.zip . ; \
		cd ../ ; \
	done ;

# deploy: $(call check_defined, STACK_NAME)
deploy:
	aws s3 sync build s3://aws-dms-to-hudi-example/artifacts/ --exclude '*' \
	--include '*.zip' --include '*.yaml' --include 'stepfunctions/**' --delete  ;

enable_jdbc:
	RULE=`aws --region $(REGION) ssm get-parameter --name /$(STACK_NAME)/emr_pipeline/event_rule/jdbc_load/name | jq -r .Parameter.Value` ;\
	aws --region $(REGION) events enable-rule --name $$RULE && echo "enable_jdbc complete"

enable_incremental:
	RULE=`aws --region $(REGION) ssm get-parameter --name /$(STACK_NAME)/emr_pipeline/event_rule/incremental_hudi/name | jq -r .Parameter.Value` ;\
	aws --region $(REGION) events enable-rule --name $$RULE && echo "enable_incremental complete"
