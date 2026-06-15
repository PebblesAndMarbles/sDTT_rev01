= About This Document =

== Scope ==

This document covers advanced concepts and techniques that apply to any Adaptive Manufacturing model/capability. It covers capabilities that are available as of AM Server 2.3.2, and makes no distinction of which capabilities were changed from, or unavailable in, earlier versions of AM Server.

== Intended Audience ==

This document is written for those responsible for editing AM models, be it data edit or model (structure) edit. If you are only making minor edits to an existing model – such as changing the recipe name for a given layer – then the WBT courses should be sufficient for your needs.

== Additional References ==
* Adaptive Manufacturing Basic Training Web Based Training - http://ease.intel.com/Saba/Web/Main, ID 01112831
* Adaptive Manufacturing Configuration Guide for Model Architects Part II: AM Recipe Selection – not yet released
* Adaptive Manufacturing Configuration Guide for Model Architects Part III: AM Lot Selection – not yet released
* Adaptive Manufacturing Configuration Guide for Model Architects Part II: AM Auto-RFC – not yet released

== Contents ==

* The '''Model Mapping''' section describes how AM Framework locates which model to use for runtime transactions.  
*  
* The '''Distributed Models''' section describes how tables can be shared by multiple models. 
*  
* The Variables section describes how variables are modeled and used in your models. 
*  
* The Key Specification section describes how to implement pattern matching in your lookup tables, and how to effectively set up your tables to avoid expensive lookups. 
*  
* The Value Specification section describes how to configure output values in your lookup tables, including table references and function string syntax. 
*  
* The Admin Functions section describes what admin functions are and how to engage with your automation rep to configure them for your model. There is also a list of standard admin functions provided out-of-the-box. 
*  
* The Model Validation section contains the list of rules used by AM model validation, and describes how to you can work with your automation rep to alter the model validation behavior, including for specific rules. 
*  
* The List of Available Runtime Functions contains the list of all the runtime functions that are available to you out-of-the-box. Each function comes with a specification of the expected parameters, a description of the function behavior and return value, and sample usage.

= AM Model Mapping & Security =
This section describes how AM Framework locates which model to use for specific runtime transactions. Whenever a new model is added to AM, the user and/or their automation resource will have to make some configuration changes to get transactions routed correctly to the new model.

== Two-level ENTRY models ==
AM Framework goes through two levels to locate which model to use for a given transaction (seeFigure 1). First, it starts at the Fab-level ENTRY model, which specifies which area and process owns the model for this transaction; Automation owns the configuration for this level. Second, it goes to the area-level ENTRY model, which specifies which model should process the transaction; the area owns the configuration for this level.

[[File:AM Model Ref ENTRY.png]]

Figure 1: Two levels of ENTRY

== Fab-level ENTRY model ==

Automation owns the configuration of the Fab-level ENTRY model. The general best-known methods for configuring this model are as follows:

* For recipe selection routing (especially for process tools), use ENTITY. If possible, use the first-three characters with a wildcard (e.g., XYZ*) where possible. If specific entities need to be split out (e.g., running multiple processes in the factory), only specify the prefix once (e.g., XYZ(101|102)) to improve AMServer performance. 
* For lot selection routing, use operation details: 
** Use the AREA_MODULE to map to the area, so as to avoid having to configure every operation separately. Figure 2 below shows where to find the area module in Operation Detail View. In this example, the area module is “DM-ORL”. If possible, use wildcarding; here, we could use “DM-*” to redirect all Defect Metro operations to the Defect Metro area with just a single configuration row. 

[[File:AM Model Ref AREA MODULE.png]]
Figure 2: AREA_MODULE in Operation Detail View

** Use the OPER_PROCESS (which is the operation process type) to determine which process owns the AM model.
** The SHORT_DESC and LONG_DESC variables (which are the operation short and long descriptions) should be used sparingly, and only in those cases where AREA_MODULE is not sufficient and configuring by OPERATION is not sustainable.

== Area-level ENTRY model ==

Each area in the AM Area Tree (including the owning process, e.g., D1D.Lithography.1268) has an ENTRY model, which consists of a table also called ENTRY. Unlike other AM tables, only standard context variables may be used in your ENTRY table (e.g., you cannot use a custom variable called “LAYER”). See Variables  for the list of standard context variables. The best-known methods for configuring this model are:

* For recipe selection, especially process tools, use ENTITY.
* For lot selection, use the AREA_MODULE to map to the module’s model, so as to avoid having to configure each operation separately. Figure 2 above shows where to find the area module in Operation Detail View.
* If you use different models for recipe selection and lot selection, add the _DOMAIN variable as a key (with values “RECIPE” for recipe selection and “SKIPLOT” for lot selection). AM Framework will automatically populate _DOMAIN based on what type of transaction is being processed.

Below is what an example metrology area-level ENTRY table might look like  (note that “[FAB]” can be used to represent the Fab name in the path; this will make your tables easier to transfer to other sites):
{| class="wikitable"
|-
! AREA_MODULE !! _DOMAIN !! MODEL_NAME !! PATH
|-
| * || SKIPLOT || LotSelection || [FAB].DefectMetro
|-
| DM-CLF || RECIPE || CLASSIFY || [FAB].DefectMetro.Classify
|-
| DM-BPA|DM-BFSCAN || RECIPE || BFSCAN || [FAB].DefectMetro.BFSCAN
|}
Table 1: Example area-level ENTRY table

== Security ==

Each leaf in the AM Area Tree (i.e., path.process) is mapped to a Rialto role prefix in order to control access to your model.  When a user attempts to do something with that model via AMUI, AM Server appends a suffix to the prefix and checks whether the user has that role. The suffixes (with typical privileges listed) are:

* _VW: View privileges (view the list of models; open any version of any model)
* _DE: Data Edit privileges (view privileges plus: check out/in a model; edit table configuration)
* _ME: Model Edit privileges  (data edit privileges plus: edit table structure; create table)

If the user has the view privileges for a node in the tree higher in the hierarchy, then the view privileges are cascaded down. This is not true of the edit privileges.

Users also have the option of mapping an individual model to a separate Rialto role prefix than the one the node is mapped to. Some fine points:

* Models that don’t have their own role prefix will use the role prefix configured at the node level. Note that this means existing models are not impacted by this change.
* Users should be able to access the model in AMUI as long as they have a role that allows them to access that specific model. The same is true if they have a role required to run any admin function defined for that model; for these users, if they do not have a view role, they will be able to see the models listed which they can access, and they can “open” the model to run admin functions, but will not be able to view the contents of any tables.

AMCT model security role mapping can be given a site prefix (e.g., “D1D_AM_...”). 
* For all View/DataEdit/ModelEdit access attempts, AM Server will enforce that, in addition to the model’s AMCT role, the user has the generic VL role.
* If a question mark is added to the prefix (e.g., “D1D?_AM_...”), then the VL role will not be checked for View access, just DataEdit/ModelEdit access.
* ENTRY models will always have the VL role check skipped for View access, whether or not a question mark was added to the role prefix.

=== Standalone Model Edit Security Group ===

Each node/model can be optionally assigned a separate Model Edit security group. This is in addition to the standard Model Edit security group (i.e., Rialto prefix + “_ME”); a user with either one has full Model Edit privileges. Just like admin function security groups, this standard Model Edit security group does not need to follow any convention; it can literally be any group. There are several drivers for this security group, including simplifying the area’s AGS submissions by creating a smaller set of Model Edit groups (perhaps as few as one), rather than one for each Data Edit group. 

Work with your Automation rep to have this configured. Note that this group is not currently visible in AMUI, as is the standard group (in the Object Properties panel).

== Tabel-level Accessibility ==
	
Each AMCT table has an Accessibility property with one of three values, which affects how the View, DataEdit, and ModelEdit roles can interact with that table. The table below describes how the different roles are affected by Accessibility.

{| class="wikitable"
|-
!  !! Role = View !! Role = DataEdit !! Role = ModelEdit
|-
| Accessibility = Data || Can view || Can view and edit || Can view and edit
|-
| Accessibility = AppVisible || Can view || Can view || Can view and edit
|-
| Accessibility = AppHidden || Cannot view || Cannot view || Can view and edit
|}

Table 2: Table-level Accessibility behavior 

AMServer restricts DataEdit usage for VARDEF tables with Accessibility of Data. Users can modify certain columns (e.g., “LOOKUP_VALUE”), but there are other columns they cannot modify, nor may they add or delete rows. Users with ModelEdit privileges have full access as with other tables.

Data tables can be modified to require ModelEdit privileges to mark the T (Transfer) column with N or P values. This checkbox appears on the Edit Table Properties dialog in AMUI: “Model Edit role required to set T column to N or P”.

= Distributed Models (HOST_DEF, CLIENT_DEF) =

AM Distributed Models enable the user to:
* '''Reuse tables for multiple models''' – for example, an area may define a mapping of operation-to-layer that is used by all modules within that area;
* '''Implement table-level security''' – By putting different tables in different models, the user can control who can edit which tables.

The diagram below shows distributed models in action. Here, the host model – MODEL1 – contains a table called MYTABLE and grants access to this table to the client model by listing it in HOST_DEF. The client model – MODEL2 – specifies the location of the table in CLIENT_DEF, and then references it in VAR_DEF the same way it would reference it as if it resided in the client model itself.

[[File:AM Model Ref DistributedModel.png|50*100px]]
<br>
Model Validation will always enforce that edits to a host will not break a client, and vice versa:
* Model Validation of an InEdit host model will also validate the Active versions of all client models listed in HOST_DEF;
* Model Validation of an InEdit client model will also validate the Active versions of all host models listed in CLIENT_DEF.

This means that, if you are adding a new table to a host and a reference to that table in a client, you will need to check in the host model first.

=== Local References for Distributed Models ===

When a host’s table is being resolved, all references in that table must exist in the host’s model:
* Variables must exist in the host’s VAR_DEF, and do not need to be in the client’s VAR_DEF;
* Table references must point to tables that exist in the host model or in a model to which the host is linked (i.e., the host would have both a HOST_DEF and CLIENT_DEF).
* Custom functions must exist in the host’s FUNCTION_DEF, and do not need to be in the client’s FUNCTION_DEF;

=== Gotchas & Limitations ===

* As of AM 2.1.3, models can only be checked in one-at-a-time. If you introduce a circular dependency, you won’t be able to check either model in. For example, if you change the name of an output column in the shared table, then you cannot check in the client first (as it will be validated against the active host, which has the original column name in the shared table), nor can you check in the host first (as it will be validated against the active client, which refers to the original column name). You may be able to come up with a workaround for this; in this example, you could duplicate the column so that the host temporarily has both names, which will allow you to check in the host, then the client, then check out the host again and remove the obsolete column. Alternatively, you can have Automation temporarily turn off the “validate on check-in” feature, although you incur the risk of a transaction being processed against incompatible versions of the host and client within a brief window.
 
== Configuring HOST_DEF == 

You will need to add one line to HOST_DEF each time you are granting access of a table to a client model (i.e., wildcarding is not permitted). For example, if you are granting access of two tables to three client models, you will need six lines in HOST_DEF. To ease transfer, [FAB] can and should be used instead of the site name in PATH.

{| class="wikitable"
|-
! Field !! Type !! Description
|-
| TABLENAME || Input || The name of the table in this model that you are allowing other models to use
|-
| PATH || Input || The path to the location of the client model to which you are granting use of the table (e.g., [FAB].Lithography.CDSEM)
|-
| PROCESS || Input || The process of the client model to which you are granting use of the table (e.g., 1272). If blank, AMServer will use this model’s process when navigating to the client.
|-
| MODELNAME || Input || The name of the client model to which you are granting use of the table
|}

Table 3: HOST_DEF fields

== Configuring CLIENT_DEF == 

You will need to add one line to CLIENT_DEF for each table you are accessing that resides in another model. To ease transfer, [FAB] can and should be used instead of the site name in PATH.
{| class="wikitable"
|-
! Field !! Type !! Description
|-
| TABLENAME || Input || The name of the table that you are accessing from another model
|-
| PATH || Input || The path to the location of the host model which contains the target table (e.g., [FAB].Lithography)
|-
| PROCESS || Input || The process of the host model which contains the target table (e.g., 1272). If blank, AMServer will use this model’s process when navigating to the host.
|-
| MODELNAME || Input || The name of the host model which contains the target table
|}

Table 4: CLIENT_DEF fields

= Variables & Variable Definition Tables =

Variables are used as a standard convenience to hold a value that can be used elsewhere in your model. A variable can be used as an '''input column''' for most tables; it can also be used in '''function specifications'''. There are three types of variables: ''standard context variables, custom context variables, and capability variables''.

As each transaction is processed for your model, AM Framework passes around a dictionary called the '''context''' used by various features. The context is initialized with variables passed by other systems (e.g., lotid.). AM may automatically populate the context with missing information (e.g., operation process/module). These constitute the standard context variables. As custom context and optional variables are referenced, they will be evaluated and the results stored in the context. For the GetRecipe and DetermineSkip calls, the final contents of the context are recorded in HISTORY_RECIPE and HISTORY_SKIPLOT respectively.

== Standard context variables == 

Standard context variables are those that are sent with, or can be derived from, the original transaction request. The set of standard variables includes :

{| class="wikitable"
|-
! Variable !! When valid !! Notes
|-
| LOTID	|| Recipe & lot selection ||
|-
| ENTITY || Recipe selection ||
|-
| OPERATION || Recipe & lot selection ||	For lot selection, this represents the target metro operation.
|-
| PRODUCT || Recipe & lot selection ||
|-
| ROUTE	|| Recipe & lot selection ||
|-
| REWORK_NUMBER	|| Recipe & lot selection || This is the value of lot attribute 10000, “ReworkNumber”. 
It is not the rework number that appears in the LotDetail grid.
|-
| OCCUPIEDSLOTS	|| Recipe & lot selection || The comma-separated list of occupied slots
|-
| SHORTWAFERIDS	|| Recipe & lot selection || The comma-separated list of short wafer ids
|-
| AREA_MODULE	|| Recipe & lot selection ||	See Figure 2 
|-
| OPERPROCESS	|| Recipe & lot selection ||	The MES process for the target operation.
|-
| SHORT_DESC	|| Recipe & lot selection || The MES short description for the target operation.
|-
| LONG_DESC	|| Recipe & lot selection || The MES long description for the target operation.
|-
| OWNING_PROCESS || Recipe & lot selection || The process node in which the target AM model resides (e.g., “1272”)
|-
| _DOMAIN || Recipe & lot selection ||	RECIPE for recipe selection, SKIPLOT for lot selection.
|-
| SOURCE || Recipe & lot selection || Source system that is making the AM call. Values include “NTSC”, “SC.NET”, “MES”, “DSS”, “PX”.  . 
If you think you need to use this variable, you should review your plans with your Automation rep.
|-
| VIRTUALLINE || Recipe & lot selection	|| Which fab owns the lot (lot UDA VirtualLine)
|-
| CURRENTSITE || Recipe & lot selection	|| Where the lot is located (lot UDA CurrentSite)
|-
| ALLOWEDVIRTUALLINE||	Recipe selection || What WIP is allowed to run on the tool (entity category AllowedVirtualLine); 
like all multi-value categories, AM will convert to a comma-separated list, e.g. “D1C,D1D”)
|-
| OWNEDBY ||	Recipe selection ||	Which fab owns the tool (entity category OwnedBy)
|-
| _SEQ	|| Recipe & lot selection || This will always have the value “1” except for AMPX (SOURCE=PX) l-tuples, for which will set it equal to how many operations downstream OPERATION is from the lot’s current position (where “1” represents the current operation). If you think you need to use this variable, you should review your plans with your Automation rep.

|}	

Table 5: List of standard context variables

 
== Custom context variables == 

Custom context variables are those that are defined in a variable definition table. This is any table in your model that either has the name “VAR_DEF”, or is of type VARDEF. A model may have its variables defined in different tables; one or more of these tables may even be stored in a different model (see Distributed Models). Henceforth, the phrase “VARDEF tables” will be used to represent both tables of type VARDEF and tables with the name “VAR_DEF”.

Custom context variables can be used in the same way as standard context variables. For example, you may define a variable called “LAYER” to hold the layer specification for a given transaction. This variable can then be used a field in a lookup table, or in a function. Using custom variables will reduce the size of, and give more clarity to, your lookup tables. The Value Specification section describes the different methods for assigning a value to a custom variable.

=== Rules and guidelines for custom context variable names ===

* Variables must be alphanumeric, but can also use the underscore (‘_’) character.
* Variables must not start with a numeric character.
* Variables must be defined in ALL_CAPS; separate words with the underscore character.
* Custom variables should not start with an underscore, as these have a greater likelihood of clashing with internal context variables used by AM Framework.

== New standard variable _SPC_SOURCE ==

For FABs that are running both SPC# and SPC++, you may require model-level control over which system is being used for AM’s SPC calls, overriding your site’s default setting for AMServer.

If the variable _SPC_SOURCE is defined and set to “SPC++”, then AM will attempt to make calls to SPC++. Otherwise, AM will use the configured default, which is controlled by site Automation.

== Capability variables == 

Finally, most of the AM Framework capabilities implement standard algorithms that require the presence of specific variables. Some of these variables are optional; if not defined, then AM Framework will use a hard-coded default value. The capability-specific reference documents list the required and optional variables for each capability.

* Example 1: The lot selection capability requires APF_CLUSTER to be defined in order to run in-queue reevaluation (IQR) for queued lots.
* Example 2: The wafer selection capability will look for an optional FORCE_EXCLUDE variable to be defined. If so, then the specified waferids will be excluded for consideration.

== Lazy Evaluation == 

In general, AM Framework will avoid resolving variables unless required. See the Key Specification & Lazy Evaluation
and List of Available Runtime Functions (e.g., “If”,”Or”) sections for examples where variables may be referenced but not necessarily evaluated. For this reason, you may not see the value of each variable defined in your variable definition table(s) in the CONTEXT field of your history tables.

== Cached Context ==

The primary use of cached context is to store the results of a static lookup in order to avoid excessive lookups. Both the Auto-RFC and lot selection features will result in your model being evaluated multiple times for a given lot/operation. By caching the variable value, the lookup will be read from the cache in future evaluations instead of being reevaluated. While you will get minor performance benefits for doing this for variables defined in AM lookup tables, the primary benefit will be for variables that depend on external systems (e.g., GetLotAttribute()). See Configuring a Variable Definition Table
for how to mark a variable as cacheable.

If a variable has an incorrect value cached for a given transaction, you will need to use the DeleteLotHistory admin function to clear the cache for that transaction.

'''Warning'''

If an AM-enabled module is doing F4 introductions at an AM-enabled operation, it may pick up cached context from the F3 introduction (or vice versa, depending on the order of the introductions). For this reason, it’s best to avoid using CACHED=’Y’ is there is a risk of the lot being subjected to both an F3 and an F4 at that operation/route.

== Intra-transaction vs. Inter-transaction caching ==

Variables marked with ‘Y’ are evaluated only once ever, with the primary driver being transaction performance. The value ‘T’ will ensure that a value is evaluated only once for a given transaction, but will be reevaluated in future transactions.

{| class="wikitable"
|-
!   !! CACHED=’N’ !! CACHED=’T’ !! CACHED=’Y’
|-
| First reference within a transaction ||	Evaluate anew ||	Evaluate anew	|| Load value from last transaction
|-
| Subsequent references within a transaction (non-loop)	|| Do not reevaluate	|| Do not reevaluate	|| Do not reevaluate
|-
| References within a loop ||	Evaluate anew	|| Do not reevaluate ||	Do not reevaluate
|}

For example, let’s imagine you had a recipe element within a process job called RandomInt that used the value of a variable called RANDOM_INT, and the variable uses one of the new Random functions to generate a value. If you have a recipe lookup consisting of multiple process jobs, then the behavior will vary based on the value of CACHED used for the variable RANDOM_INT:

* CACHED=’N’: Within a given lookup, each process job could have a different value for RandomInt.
* CACHED=’T’: Within a given lookup, each process job will have the same value for RandomInt. Different lookups will generate new values for RandomInt.
* CACHED=’Y’: Within a given lookup, each process job will have the same value for RandomInt. Subsequent lookups will always use the same value that was generated with the first lookup.

== SIF Prevention ==

All VARDEF tables may have an optional output field called “ALLOW_SIF”. This field is only used when processing GetRecipe calls for recipe selection models. If the field is defined, and the cell is set to “FALSE” for a variable with an override attempt outside of AM (e.g., RCT; SIF; DMOQ), then AM will fail the recipe overlook; an error message (“Variable [name] is not allowed in SIF") will be returned to the client attempting the lookup.

More details on the use of ALLOW_SIF will appear in the upcoming Recipe Selection Reference.

== Configuring a Variable Definition Table ==

Field	Type	Description
LOOKUP_NAME	Input	The name of the variable
LOOKUP_VALUE	Output	The value of the variable (see Value Specification).

CACHED	Output	Whether or not to cache the variable for the given context (lot/operation/route/rework number)
ALLOW_SIF	Output
optional	For recipe selection models, whether or not the variable can be overridden external to AM (e.g., RCT, SIF, DMOQ).

{| class="wikitable"
|-
! Field !! Type !! Description
|-
| LOOKUP_NAME	|| Input ||	The name of the variable
|-
| LOOKUP_VALUE	|| Output ||	The value of the variable (see Value Specification).
|-
| CACHED  || Output	|| Whether or not to cache the variable for the given context (lot/operation/route/rework number)
|-
| ALLOW_SIF ||	Output (''optional)'' || For recipe selection models, whether or not the variable can be overridden external to AM (e.g., RCT, SIF, DMOQ).
|}

= Key Specification =

== Keys vs. values ==

There are two types of columns in each AM table: '''keys''' and '''values'''. Keys are input columns; they are used to determine which row to match when performing a run-time lookup. Values are output columns, and contain the results of the lookup. The format for these two types of columns is very different; the key columns contain patterns which get matched against actual values at run-time, whereas the value columns contain expressions which are dynamically evaluated at run-time. This section describes the rules for configuring patterns in the key columns; the next section (Value Specification) describes the tools available for configuring expressions in the value columns.

== First-fit pattern matching ==

AM uses a first-fit pattern matching algorithm, whereby the system works through each row, one-at-a-time, until a match is found. There are two options for ordering rows: AutoSort, and RowOrder Sort. AutoSort is the BKM, and RowOrder Sort is on the roadmap to be deprecated; there are no drawbacks to using AutoSort, and RowOrder Sort has potential for non-obvious user mistakes.

In AutoSort, a table is sorted by its key columns; ROW_ID is ignored, but ROW_ORDER is always the first column, and the other key columns are ordered by their Column Order (which the user can modify via the Structure tab). In RowOrder Sort, a table is sorted by ROW_ORDER, then by ROW_ID (and maintaining the ordering of ROW_ID between table modifications is not guaranteed).

No matter which sort algorithm is being used, the order that you see in AMUI is the sorted order; AM Server will do a first-fit based on the sorted order.

In AutoSort, within a column, AM sorts the values based on a custom algorithm that takes into account the full range of key wildcarding options.
* All values without wildcards come before values with wildcards;
* “*” comes after all values;
* Aside from that, values are compared by character position, left-to-right
** Alphanumeric characters are sorted alphabetically
** ‘(‘ comes before all alphanumeric characters
** Alphanumeric characters come before all other wildcard characters
** Wildcard characters are sorted in the order '|', '[', ']', '^', '?', '*'.

   ABC101
   ABC111
   DEF101
   ABC(101|102)
   ABC101|ABC102
   ABC10[123]
   ABC10[^456]
   ABC10?
   ABC10*
   ABC*
   DEF*
   *
Figure 3: AutoSort Example

== Wildcard patterns ==

First-fit resolution enables AM to provide more wildcarding than baseline Automation systems. The AM wildcard system is a hybrid of the standard regular expressions format and the well-known globbing characters ‘*’ and ‘?’. The following table shows examples of the most common wildcarding techniques.

{| class="wikitable"
|-
! Technique !! Description !! Example Usage
|-
| ?	|| Matches any character ||	Px?123
|-
| *	|| Matches any sequence of characters ||	*a123
|-
| [...]	|| Denotes a set of possible character matches	|| Px[abc]123
|-
| [^...] || Matches every character except the ones listed || P?[^def]123
|-
| <nowiki>(...|...|...)</nowiki>	|| Denotes a set of alternate possibilities || <nowiki>P(xa|yb|yc)123</nowiki>
|-
| [a–z]	|| Denotes a range of possible matches	|| Px[a–c]123
|-
| [^a–z] || Denotes a negative range of characters || P?[^d–f]123
|}
Table 6: AM Wildcard Techniques
''All examples match the value Pxa123, but not Pyd123'' 

=== Wildcarding techniques not supported and other gotchas ===

There are other common regular expression techniques available, but ones we do not expect to see in AM configuration. In order to support the globbing characters, and in order to keep AM configuration intuitive and non-cumbersome, AM does not support the following:

Technique	Details
.	Use the globbing character ‘?’ instead to represent any character. AM will assume you mean a literal period, and thus replace ‘.’ with “\.”.
^, $, partial string matching	AM automatically bounds all patterns with ‘^’ and ‘$’ to ensure the full string is always matched.
\	You will need to escape any characters used by AM wildcarding if you expect them to appear in the actual string (e.g., \*; see below for full list). For this reason, if your pattern needs to contain a literal backslash, you will need to escape it: “\\”.

{| class="wikitable"
|-
! Technique !! Details
|-
| .	|| Use the globbing character ‘?’ instead to represent any character. AM will assume you mean a literal period, and thus replace ‘.’ with “\.”.
^, $, partial string matching	AM automatically bounds all patterns with ‘^’ and ‘$’ to ensure the full string is always matched.
|-
| ^, $, partial string matching ||	AM automatically bounds all patterns with ‘^’ and ‘$’ to ensure the full string is always matched.
|-
| \ || You will need to escape any characters used by AM wildcarding if you expect them to appear in the actual string (e.g., \*; see below for full list). For this reason, if your pattern needs to contain a literal backslash, you will need to escape it: “\\”.
|}
Table 7: Wildcard Techniques not supported

The following characters need to be escaped if you expect them to be matched literally: *, “, |, (, ), [, ], +, {, }, \.

== Key Specification & Lazy Evaluation ==

While column order doesn’t impact which row gets selected, it can impact the performance of your model. AM employs lazy evaluation, whereby it only evaluates variables as they are needed. As AM evaluates each row left-to-right when attempting to match, placing “expensive” key variables to the right of other keys (as controlled by COLUMN_ORDER) takes advantage of lazy evaluation.

	Example:

        @VAR_DEF
                LOOKUP_NAME	LOOKUP_VALUE      	         CACHED
                MY_ATT		=GetLotAttribute(“CustomAtt”)	  Y

        @RCP_RECIPENAME
                     ROW_ORDER	       PRODUCT	         MY_ATT	       RECIPENAME   
                        1	        ProductX	 val1		rcp1
                        1		ProductX	 val2		rcp2
                        2		      *		  *		rcp3
		
For any product other than ProductX, there is no need to get the lot attribute from MES, as the recipe will always be rpc3. Since PRODUCT is listed before MY_ATT, AM will be lazy and skip evaluating MY_ATT.

Model Validation rule 1404 will help ensure that your model is taking advantage of lazy evaluation.

 
== Data Type for Keys ==

All columns for AM tables have a data type assigned to them (e.g., string, integer, etc.). However, all key specifications that employ special characters from regular expressions or globbing are stored as strings, no matter what the underlying data type is. These columns should then be defined with the data type STRING. 

For example, if you are going to configure one operation per row in a lookup table, then you can configure the column OPERATION to be of data type INTEGER to make sure valid integers are being entered. However, if you are going to group operations together (e.g., a key specification of “3847|3859” in order to keep operations 3847 and 3859 on the same line), then you must configure it to be of type STRING, with a configured data length long enough to store your regular expressions.  

== _EXPIRES ==

_EXPIRES is an optional standard key column that can be deployed in any LOOKUP table. 
* The _EXPIRES column should be of type STRING, and values should be entered in YYYY-MM-DD format. AM Server will validate that input is properly formatted.
* Users may configure a date in each row, or otherwise use ‘*’ as with other key columns. 
* If configured, then, once past the date, AM Server will ignore the row as if it were not there at all in it’s top-down scan algorithm. The row will still be visible in AMUI for the user to update/delete later.

== Model-specific filter keys ==

To help reduce AMPX workload and improve cert update latency, you may be asked to configure model-specific AMPX filter keys by adding a model setting (see “Configuring a Model Settings table”) AMPX_FILTER_KEYS. Its value should be a comma-separated list of variables (e.g, “LAYER,CHEMICAL”) that are used as custom key columns in your model’s tables. You should only configure such keys if they will significantly reduce workload; it is common for other standard key column variables (OPERATION, ENTITY, etc.) to sufficiently narrow down which AMPX certs get evaluated on model check-in. You can work with your Automation support rep to review model check-in history to identify such opportunities.

== Configuring the Model Settings table (@_MODEL_SETTINGS) ==

{| class="wikitable"
|-
! Field !! Type !! Description
|-
| LOOKUP_NAME || Input || The name of the model setting. Currently only AMPX_FILTER_KEYS is supported.
|-
| LOOKUP_VALUE	|| Output || The value for the setting
|}

= Value Specification =

'''Warning'''

''While there are very few standard AMCT tables that handle case insensitivity and whitespace, generally it is best for the table editor to be conservative and assume that all tables are sensitive to both case and whitespace.''


There are four different ways to specify a value: literal, table reference, variable reference, and function strings. Below is a VAR_DEF table with all four examples:

     LOOKUP_NAME		LOOKUP_VALUE
      VAR1			Hello
      VAR2			@TABLE1
      VAR3			=VAR2
      VAR4			=Concat(VAR1,”AND”,VAR3)

== Literals ==

Literals represent actual values. In the example above, VAR1 is assigned the value “Hello”. If a value specification is not one of the other types below, then it will be treated as a literal. Note that this is simply shorthand for =”Hello”.

== Table references ==

A prefix of ‘@’ redirects the lookup to another table. There are two valid formats for table references:

* @TABLE – In this case, the variable being assigned to must exist as an output field in the designated table. In the example above, TABLE1 must contain an output field with the name VAR2. Note that all lookup tables – not just VARDEF tables – can contain table references; in such cases, the variable being assigned to is the name of the field containing the table reference (i.e., both the current table and the referenced table must share this same output field).
* @TABLE.FIELD – In this case, the variable being assigned to does not have to exist as an output field in the designated table. Rather, the output field which appears after the ‘.’ in the table reference will be used.
Example:

       @VAR_DEF
              LOOKUP_NAME	LOOKUP_VALUE      	    CACHED
               MY_VAR		@TABLE1		              Y

       @TABLE1
              ROW_ORDER	        PRODUCT	        MY_VAR
                 1		ProductX	 @TABLE2		
                 1		ProductY	 @TABLE2.ALT_VAR		
                 2		     *		  val1		

        @TABLE2
               OPERATION	MY_VAR		ALT_VAR
                 2000		 val2		alt2		
                   *		 val3		alt3		

* When PRODUCT=ProductX and OPERATION=2000, MY_VAR will be assigned “val2”.
* When PRODUCT=ProductY and OPERATION=2000, MY_VAR will be assigned “alt2”.

== Table References in Function Strings ==

Table references can also be used from within function strings. Note that this requires that you specify the field (.FIELD) being referenced. In the example below, MYTABLE should contain a value field called VAR.

    =Add(@MYTABLE.VAR,5)

'''Notes:'''
Table references in function strings will not appear in the CONTEXT field of your history table. If this is desired, then you should use a variable to hold the table reference.

== Variable references ==

The ‘=’ in =VAR2 tells AM to assign the value of one variable to another. Note that this is merely a shortcut for the function string =Echo(VAR2).

== Function strings ==

* The ‘=’ followed by parenthesis tells AM to evaluate the function string and assign the result to the variable. Some notes for function strings: 
* String literals in function strings must be quoted (see “AND” in the VAR4 example). This is needed so that AM can distinguish string literals from variables. Numeric literals do not have to be quoted. 
* You can use variables inside of function strings (see the use of VAR1 and VAR3 in the VAR4 example).  
* Nested functions are allowed. Example: =Max(0,Sub(NUM1,NUM2)) 
* See the section List of Available Runtime Functions for a list of standard Framework functions; these can be used in any model without any additional configuration needed. 

*You can ask your Automation contact to write custom functions for you. You may need to use a custom function if your model requires:
**	Input from unique data sources;
**	Unique algorithms that AM Framework does not provide;
**	Desire to hide complexity from the AM model.
Custom functions must be referenced in the standard table FUNCTION_DEF. For more details on configuring FUNCTION_DEF, see Appendix A: LOT table BKMs

== Optional Parameters in Function Strings ==

Some standard Framework functions have support for optional parameters. Such parameters will have default values provided if unspecified. Note that custom functions written for your model by your area developer currently do not support optional parameters.

The following examples for the use of optional parameters all use the following function:

      public static string Reverse(string list, string delimiter = ",", string join = ",")

Here, the first argument – list – must always be provided. The second two parameters – delimiter and join – are optional; in both cases, the default value of “,” will be used if they are missing:

        Reverse(“a,b,c”) 		->	“c,b,a”
        Reverse(“a:b:c”, “:”)		->	“c,b,a”
        Reverse(“a:b:c”, “:”,”::”)	->	“c::b::a”

To make it easier to use functions that contain multiple optional parameters (such as Reverse), AM supports '''named parameters'''. By specifying the name of the parameter, followed by a colon, before the argument value, you are telling AM Server to match up the value as you intend:

        Reverse(“a,b,c”, join:“:”)	->	“c:b:a”

The use of named parameters isn’t restricted to just optional parameters. You may use this feature to make your function strings self-documenting. You can also see in the second example below that the order of parameters can be modified when named parameters are used:

       Reverse(“a:b:c”, delimiter:“:”,join:”::”)		->	“c::b::a”
       Reverse(delimiter:“:”,join:”::”,list:“a:b:c”)		->	“c::b::a”

 

== Hash References ==

'''Note:''' AM now supports marking an object with the CachedLookup property that will give you the benefits of hash references without having to explictly enter them in your model configuration. This section should be considered obsolete, though the concepts live on in CachedLookup.s


If you have a custom table called TABLE with output fields FOO and BAR, typically you would access these fields using the expressions @TABLE.FOO and @TABLE.BAR.

       @VAR_DEF
             LOOKUP_NAME	LOOKUP_VALUE      
                FOO		@TABLE	
                BAR		@TABLE	

A hash reference represents a successful table lookup, and can be used in place of the name of the table. In the above example, you could define a variable called TABREF and set it to #TABLE in your variable definition table. You could then replace the references above with @TABREF.FOO and @TABREF.BAR. The only difference is that the matching row in TABLE will only be determined the first time you use TABREF; further references to TABREF will immediately go to the previously matched row.

        @VAR_DEF
             LOOKUP_NAME	LOOKUP_VALUE      
                TABREF		#TABLE
                FOO		@TABREF	
                BAR		@TABREF

The main drivers for using a hash reference are:

# If a table lookup is done within a lambda expression, but multiple output fields are needed, it’s easiest to write a single lambda expression and then access other fields via the table reference.
# If you have a table that is both long and wide (multiple output fields), you can get big performance settings by replacing your table reference with a hash reference.

'''Note:''' Defining a hash references can only be done in a variable definition tables, and unlike table references, not as part of a larger expression.

== Data Type for Values ==

All columns for AM tables have a data type assigned to them (e.g., string, integer, etc.). However, all value specifications that employ anything other than literals are stored as strings, no matter what the underlying data type is. These columns should then be defined with the data type STRING.  
  
For example, if you have a lookup table which outputs an integer value, then you can configure that column to be of data type INTEGER to make sure valid integers are being entered. However, if you are going to redirect the lookup to another table (@TABLE2) or specify a computational function (=Multiply(VAR2,1.5)), then you must configure it to be of type STRING, with a configured data length long enough to store your regular expressions.

= Admin Functions =
Admin functions are custom AMUI actions you can run for your model. Typical categories of admin functions:
* Configuration validation (e.g., Single Recipe Lookup) – Used to validate the table setup at the point of configuration, perhaps before checking in.
* Guided data entry (e.g., Insert Record) – Used to make routine data entry easier and more robust, or to expose particular data entry under a different security model than the standard Data Edit.
* Data cleanup (e.g., Delete Lot History) – Used to “reset” state for a given lot or partition, perhaps in tables not visible in AMUI.

 There are two types of admin functions:
* '''Generic Admin Functions''': These are supported by AM Framework, but still require that your Automation rep enable them for a specific model via configuration.
* '''Custom Admin Functions''': Your Automation rep can provide a script to provide any conceivable functionality not covered by a generic admin function script.

  Note for APC users: You may be used to having your APC application owner develop custom admin function scripts. AM strives to provide generic admin functions for all customer needs; the custom admin function capability is there as a safety net in case new requirements crop up.

== Admin Function Security ==

Each admin function is mapped to one or more Rialto roles. You can use any valid Rialto role, including your model’s Data View (VW), Data Edit (DE), and Model Edit (ME) roles, but note that the user will need the VW role in order to open the model at all. You can also create custom roles (and EAM entitlements) to control admin function security. The standard suffix for custom admin function roles is G1 (Group 1), G2, and G3; there is no inherent hierarchy within these groups. 

To modify your admin function security, decide which role/roles should have access to each admin function, and then inform your Automation representative.

Example: You may want anyone with the VW role to run SingleRecipeLookup, only those with the “G1” role to run InsertRecordNewLayer, and those with G1 or G2 role to run InsertRecordNewProduct. You give your requirements to your Automation representative, who configures the admin function security to match the requirements. If this is your first use of the G1 and G2 roles for your module, you will need to have them created and exposed in EAM.

== Admin Function Optional Features ==

'''Mapped variables'''

You can expose one parameter name in the admin function and map it to a different internal variable name. While this may be useful to provide more user-friendly names in the admin function window – although this will be limited by AM not supporting spaces in parameter names – it is more useful as a building block for other capabilities, such as MES UDA override below.

'''MES UDA Override'''

Previously, if a model used one of the CheckRange family of functions (which have the UDA name as one of the parameters), then the user was forced to test against the actual MES value, unless the admin function provided another way to mask that check. With this change, the EvaluateFunctionString, SingleRecipeLookup, and ToolSelection admin functions can be configured to allow users to test against simulated MES values. Note that you can use this technique for any MES Entity UDA usage, even GetEntityAttribute. 

*You must have _AMUI_INPUT_STUBS = TRUE configured as part of the admin function to enable this capability.
*If you have multiple subentities, you will have to configure a parameter for each subentity’s UDA you wish to test (e.g., PC1 and PC2 in the below example).
*The variables to override must be called _MES_ENT_UDA_target_UdaName. Target should be MOM for a parent’s UDA, or the subentity suffix for a child’s UDA. In the example below, if the entity being tested is XYZ101, then the three UDA’s being simulated are XYZ101’s FROG, XYZ101_PC1’s TOAD, and XYZ101_PC2’s TOAD; note that the names of the parameters being presented to the user in this example (PARENT_FROG, etc.) do not matter – AMServer only cares about the final mapped variable.

       <param default="TRUE" show="0">_AMUI_INPUT_STUBS</param>
       <param mappedVar="_MES_ENT_UDA_MOM_FROG">PARENT_FROG</param>
       <param mappedVar="_MES_ENT_UDA_PC1_TOAD">CHILD1_TOAD</param>
       <param mappedVar="_MES_ENT_UDA_PC2_TOAD">CHILD2_TOAD</param>

 
'''Admin Function parameter ordering'''

Previously, admin functions were always presented in AMUI in alphabetical order. You may now opt-in to have them presented in the order in which they are configured in the admin function configuration file. To do so, have Automation add the below node within the <Functions> node, in addition to configuring the admin functions to be in the preferred order.

     <sort>0</sort>


== List of Generic Admin Functions ==

For the end user, it is not overly important to know whether or not their desired functionality is covered by a generic admin function; the presence of generic admin functions is only to make Automation delivery of these capabilities quicker and more robust. Listing the available generic admin functions here is merely intended to inspire the model architect. The details of how to configure each function are not listed here, as they are controlled not by AMUI model configuration, but by an admin configuration file that Automation controls.

=== DeleteLotHistory ===

DeleteLotHistory purges a lot from several tables (one of which is not accessible from AMUI), and is used to “reset” your model for a given lot/operation. The main scenarios where this is used are:
•	AM Auto-RFC – When your model uses AM Auto-RFC, AM keeps track of how many attempted introductions there have been for your lot, and will always attempt to start wherever the sequence completed. Deleting the lot history for the lot will make AM think that it’s the first time seeing the lot, and it will start at the first step in the sequence.
•	Custom cached variables – If a lot fails to introduce because of a model that is configured incorrectly or incompletely, the lot may be “stuck” even after you fix the configuration if you have variables in your VARDEF tables with CACHED=’Y’. Deleting the history for the lot will force AM to recalculate these variables anew. 

=== SingleRecipeLookup ===

SingleRecipeLookup is used to do a recipe pre-look for a given lot. This is similar to doing a pre-look at the NTSC, except, when using AMUI, the lot need not be at the target operation. You can configure several such admin functions, selecting different subsets of {lot, operation, product, route, rework number, slots, wafers, entity} for the user to fill in; whatever does not get filled in will be assigned a default value.

Custom context variables (defined in VARDEF tables) can be exposed for the user to fill in, or have a default automatically filled in by AM Framework. This is useful if you want to test lot-generated context (such as a child lot, or a specific MES lot attribute value), but do not want to have to find a specific lot with these properties.

=== InsertRecord / ModifyRecord ===

InsertRecord is useful for controlled insertion of new records into existing tables. Each InsertRecord admin function is mapped to a particular table. AM will automatically return all required fields for the user to fill in; to have non-required fields exposed, required fields hidden, and/or hard-coded defaults (including ROW_ORDER) automatically filled in, your Automation representative needs only to make simple configuration changes.  InsertRecord actions are now captured in HISTORY_CONFIG.

ModifyRecord is useful for controlled modification of existing records into existing tables. Each ModifyRecord admin function is mapped to a particular table. Note that ModifyRecord cannot be used to add new rows; InsertRecord must be used for that. ModifyRecord actions are captured in HISTORY_CONFIG.

Unlike InsertRecord, all key fields need to be added by Automation to the admin function configuration file; it is possible to omit value fields, which will result in the omitted fields being unchanged. Note that value fields left blank by the user in the Admin Function window will be treated like blank values; the transaction will fail if the field is required, and the value will be blanked out if the field is non-required. Think twice before exposing non-required value fields via ModifyRecord.

The below enhancement apply to both InsertRecord and ModifyRecord:

*Each input parameter can be configured with a list of allowed values; if the user enters a value that is not in the list, then the request will be rejected with an appropriate error message. Work with your Automation rep to have this set up.
*Each admin function can be configured to invoke a custom script, which is run prior to committing the data. This is useful for custom validation and/or massaging the data. Work with your Automation rep to have the custom script developed and the admin function configured to invoke the script.
*The admin functions can be run against checked-out model if the target table is a LOT table; LOOKUP tables still require that the model not be checked out.

The ModifyRecord and DeleteRecord admin functions can be tagged with a wild = 1 attribute; if so, then any key field can accept wildcards, and all such matching rows will be updated / deleted.

=== EvaluateFunctionString ===

EvaluateFunctionString is useful for testing runtime functions. You may find it useful to provide multiple admin functions that leverage EvaluateFunctionString; you can configure each function to test one particular runtime, and you can even set up one to test any runtime function that the user fills out. As with SingleRecipeLookup, you can expose custom variables to be filled in, or you can have a default automatically filled in as part of the call. Work with your Automation representative to determine the best way to test your runtime functions.

=== InsertLotForForceInclude / InsertLotForForceExclude ===

If you are using AM for slot selection, and making use of the AM Framework’s force include/exclude feature, this admin function is useful for inserting lots into the table. However, it is advised you use the InsertRecord admin function, which is more fully featured.

= Model Validation =

Below is the list of model validation rules. AM will enforce that there are no errors when you attempt to freeze an InEdit model, or approve a Frozen model. If AMUI reports error and prevents you from freezing or checking in a model, click Validate Model and review the results in the Model Validation window. Note that warnings may indicate something seriously wrong with your model, so you should always validate your model and review the results prior to freezing it.

== Custom Model Validation configuration ==

You can have your Automation rep customize the Model Validation settings for your model. For each of the rules listed below, you can change the warning level (promote from warning to error; demote from error to warning) or disable it altogether. You can also configure your model to automatically promote all warnings to errors (except for those rules you have disabled).

== Custom Model Validation rules ==

AMCT supports the ability for a model to have a script that implements custom model validation rules. One such rule might be enforcing that, for a given table, two specified columns may not both be populated. There are many possibilities here; the model architect should discuss their ideas with their Automation rep. 

=== Model Validation Rule Definition Table ===

Alternatively, such rules can be written directly in the model (in addition to the C# script) in a standard table called @MV_DEF, using a slight modification of the AMCT language as described below:
* Variables are not supported; instead, a variable refers to a column name (key or value) in the table for which the rule is associated.
* Table lookups are not supported, nor are functions that require transaction context (e.g., lot/entity information). Simple self-contained functions – string, logical, list, etc. – are all available.
* Validation of each rule is done on one row at a time for the associated table. Cross-row and cross-table validation rules will require the legacy C# script implementation.
* As with the legacy model validation scripts, these are run at validation time only (e.g., model check-in), not run-time. They are no replacement for run-time checks if the final values can come from user-entered function strings or overridden by external data sources such as SIF fields. They are intended to supplement such checks to help reduce the dependency on user testing their inputs before activating the model.

=== Configuring the Model Validation Rule Definition table (@MV_DEF) ===

Field	Type	Description
RULE_NAME	Input	The name of the rule. This is never visible outside of this table.
TABLE	Output	The name of the AMCT table associated with the rule. 
BODY	Output	The expression to be validated. If the expression does not evaluate to “TRUE”, then a Model Validation error will be reported.
MESSAGE	Output	What is reported to the data editor in the Model Validation Results panel. Function strings are supported in this field.

{| class="wikitable"
|-
! Field !! Type !! Description
|-
|RULE_NAME ||	Input ||The name of the rule. This is never visible outside of this table.
|-
|TABLE	|| Output ||The name of the AMCT table associated with the rule. 
|-
|BODY || Output	|| The expression to be validated. If the expression does not evaluate to “TRUE”, then a Model Validation error will be reported.
|-
|MESSAGE || Output || What is reported to the data editor in the Model Validation Results panel. Function strings are supported in this field.
|}
Note that existence of an MV Config File or Script is visible in the AMUI Model Validation results dialog title ([S]/[C]). While the general reference does not cover AMUI behavior, this feature is primarily useful for modelers/deployers to validate collaterals are in place.

== List of Model Validation rules ==

The next two pages list all the model validation rules, with the default warning level. The code listed here will appear in the model validation output window; please include it when requesting help from AM support. The current set of model validation rules cover general model structure. The Model Validation report will give further details on the nature of the problem (e.g., for code 1301 – invalid table reference – it will tell you the name of the invalid table, as well as which table/cell contains the invalid reference).

As of AM 3.0.4, model validation focused primarily on syntax and references. On the AM roadmap is to add rules to enforce specific aspects of specific tables, particularly for Auto-RFC (e.g., LOGIC_REINTRO) and Lot Selection (e.g., SKIPLOT_LOGIC). 

{| class="wikitable"
|-
! Code !! Level !! Description
|-
| 1002	|| Error || VAR_DEF has an invalid structure. You should not get this error unless you inadvertently remove/modify a required column in VAR_DEF.
|-
| 1003	|| Warning ||VAR_DEF variable does not have proper casing. All VAR_DEF variables must follow the UPPER_CASE convention.
|-
| 1006	|| Error / Warning ||The same token is defined in VAR_DEF and RECIPE_DEF across linked models. The generated violation will be considered a warning if the two tokens are configured with the exact same string, although realize that this is no guarantee that they will be equivalent (e.g., each is defined in a table with the same name, but that table is defined locally in each model). This MV rule exists to protect your model against ambiguous overrides (e.g., SIFs). 

The rule can also be triggered if it appears in multiple variable definition tables within the same model, or if variables or recipe elements clash with user-defined function parameter names.
|-
|  1007	|| Warning || Invalid value for CACHED or ALLOW_SIF in variable definition table. A variable definition table (e.g., VAR_DEF) has an invalid value in either the CACHED field or optional ALLOW_SIF field. Valid values for CACHED are “Y” and “N”. Valid values for ALLOW_SIF are “FALSE”, “TRUE”, and “” (same as “TRUE”). 
|-
|  1102	|| Error || RECIPE_DEF has an invalid structure. You should not get this error unless you inadvertently remove/modify a required column in RECIPE_DEF.
|-
| 1103 || Warning || RECIPE_DEF recipe element does not have proper casing. The names of recipe elements should follow the PascalCase convention: (1) start with a capital letter; (2) no more than three capital letters in a row. They must also contain only alphanumeric characters. This enhances readability of the recipe in SCView.
|-
| 1104	|| Error ||Invalid Recipe Element name. Specifically, it will generate invalid XML, which can break other Automation systems.
|-
| 1106	|| Error || RECIPE_DEF is missing the recipe element CTFile. All RECIPE_DEF tables need to have a recipe element with this name.
|-
| 1107	|| Error / Warning ||The same token is defined in VAR_DEF and RECIPE_DEF.  The generated violation will be considered only a warning if the two definitions are deemed to be logically equivalent. This MV rule exists to protect your model against ambiguous overrides (e.g., SIFs).
|-
| 1108	|| Warning || Invalid value for ALLOW_SIF in recipe definition table. A recipe element definition table (e.g., RECIPE_DEF) has an invalid value in the optional ALLOW_SIF field. Valid values for ALLOW_SIF are “FALSE”, “TRUE”, and “” (same as “TRUE”). 
|-
| 1109	|| Error ||Invalid value for LOOKUP_GROUP. A recipe element definition table (e.g., RECIPE_DEF) has an invalid value in the optional LOOKUP_GROUP field. The value in the field should be the name of another recipe element definition table elsewhere in your model.
|-
| 1110	|| Error ||Invalid value for ARRAY_SPEC. A recipe element definition table (e.g., RECIPE_DEF) has an invalid value in the optional ARRAY_SPEC field. The value in the field should be the name of a variable defined in one of your variable definition tables. 
|-
| 1111	|| Error || Detected recursive recipe group definition. A cycle has been detected in the recipe element definition tables (B contains C contains B, or B contains itself) as defined by the LOOKUP_GROUP column.
|-
| 1112	|| Error ||Only one of TOOL_FILTER (table) and TOOL_ALLOWED (variable) can be defined. AM provides two solutions for tool selection, but only one may be used. The user will have to define custom variables and/or tables if they want more than one way to configure this. 
|-
| 1301 || Error	|| Table reference is not valid. Either you are referencing a table that doesn’t exist in your model, or the reference table doesn’t contain a value column required to make the reference valid (e.g., @TABLE.FIELD, where FIELD is not a value column in TABLE). The most likely cause of this error is misspelling the name of the table or the field in your reference. This same rule is applied for use of Table Access Functions (Rows, RowValue, TryLookup, GetState, etc.) when the argument is a string literal.
|-
|1302 || Warning || Variable reference is not valid. For example, you reference BOGUS, but there is no variable called BOGUS in VAR_DEF, nor is it a standard context variable. The most likely cause of this error is misspelling the name of the variable.
|-
| 1303 || Error	|| Function syntax is incorrect. For example, you are missing a closing parenthesis in the function string. The details of the syntax violation will be included in the model validation report. 
|-
| 1305	|| Error ||FUNCTION_DEF has an invalid structure. You should not get this error unless you inadvertently remove or modify a required column in FUNCTION_DEF.
|-
| 1306	|| Error || Function reference is not valid. If your model has a FUNCTION_DEF, you will receive the error “MV1306: Function [name] is not defined in FUNCTION_DEF”. If your model does not have a FUNCTION_DEF, you will receive the error “MV1306: [name] is not a valid function”.
|-
| 1307 || Error ||Function has the incorrect number of parameters. For custom functions, you need to list the correct number of parameters in FUNCTION_DEF.PARAMS. Errors with named parameters (e.g., explicating specifying a named parameter that doesn’t exist in the function signature) fall under this same code. 
|-
| 1312 || Error || CLIENT_DEF has an invalid structure. You should not get this error unless you inadvertently remove or modify a required column in CLIENT_DEF.
|-
| 1313 || Error ||CLIENT_DEF does not point to a valid table. Either the table doesn’t exist in the host model, or the host model does not exist.
|-
| 1314 || Error	|| HOST_DEF does not grant permission for CLIENT_DEF to use table. The host model needs to have a HOST_DEF table, with an entry granting the client permission to use the table.
|-
| 1320 || Warning || Invalid FUNCTION_DEF entry. User-defined functions (non-blank BODY value) should not have FILE, FOLDER, or METHOD populated; custom script functions (blank BODY value) should have FILE and METHOD populated. All of the MV1320 – 1324 rules generate warnings instead of errors due to not wanting to impact legacy models with unused yet invalid FUNCTION_DEF entries.
|-
|1321 || Error || Script for custom function does not exist. The script referenced by the FILE & FOLDER values in @FUNCTION_DEF does not exist.
|-
|1322 || Warning || Script for custom function does not compile. The script referenced by the FILE & FOLDER values in @FUNCTION_DEF exists, but cannot compile.
|-
|1323 || Warning || Method for custom function does not exist. The script referenced by the FILE & FOLDER values in @FUNCTION_DEF exists and can compile, but does not contain the function referenced by the METHOD column.
|-
|1324 || Warning || Method for custom function does not have the correct number of arguments. The script referenced by the FILE & FOLDER values in @FUNCTION_DEF exists and can compile, and does contain the function referenced by the METHOD column, but the number of its arguments does not match the number specified in the PARAMS column.
|-
|1325 || Warning ||Custom function is hidden by an AMServer function. A custom function in @FUNCTION_DEF has the same name as a standard toolkit function. Note that these warnings may arise after an AMServer upgrade, usually due to planned custom function obsolescence.
|-
|1340 || Warning || MV_DEF has an invalid structure. MV_DEF does not have all the required columns.
|-
|1350|| Error || Referenced COUNTERS table is missing. This is based on the tag passed in the counter function API.
|-
|1351 || Error || Referenced PARTITIONS table is missing. This is based on the tag passed in the counter function API.
|-
|1360 || Error || Invalid value for _PROCESS. AM Server maintains a configurable list of allowed values which can be changed as needed.
|-
|1401 || Warning ||Invalid escape sequence in key column. This may be caused if you are expecting to match a string containing a ‘\’, in which case you should escape it (“\\”) in the key spec. See the Key Specification section for more details.
|-
|1403 || Warning || Unreachable row detected. There may be a line in your lookup table that is unreachable; you will likely need to modify the ROW_ORDER values in the table to resolve this warning. This warning should not be ignored, as it could mean that your table is not going to behave as expected due to first-fit pattern matching. See the Key Specification section for more details.
|-
|1404 || Warning ||Keys defined by non-cached variables come after keys defined by cached variables. To resolve this warning, you will need to modify the COLUMN_ORDER values in your table structure definition; otherwise, you may not be taking full advantage of AM’s lazy evaluation. See the Key Specification section for more details.
|-
|1500 || Error	|| LotMeasured function called from model that does not contain or is linked to required Lot Selection tables. In order for use of this function to pass model validation, the model must either be a valid Lot Selection model (i.e., contains SKIPLOT_LOGIC or SKIPLOT_FORCE), or have a CLIENT_DEF reference to a HISTORY_SKIPLOT table residing in another model. 
|-
| 1600 || Error	|| Required columns cannot have blank values. It is legitimate to have a required column with a default blank value; this forces the user to explicitly enter something rather than accept the default. However, if such a column is added to an existing table and the table isn’t edited to fill in this new column, then future edits of the table would fail prior to AM 2.1.4, requiring Automation to step in and tweak the table. With this change, new columns can still be added, but the values for existing rows must be populated before the change can be made active.
|}
Table 8: List of Model Validation Rules

= List of Available Runtime Functions =

On the AM training website, you will find a quick reference that lists all of the standard runtime functions with brief descriptions. This section provides full descriptions and examples.

Some of the functions require that the inputs be numeric (e.g., Add). The inputs can be a numeric literal (e.g., “5”), or any reference (variable, function, table) that will evaluate to a numeric value.

Note: You may use all of these standard runtime functions without adding them to FUNCTION_DEF. Only custom runtime functions need to be configured in FUNCTION_DEF.

Optional parameters have their name underlined (e.g., “entity” in GetEntityAttribute). '''Note:''' In the PDF-to-Wiki migration of this document, the underlining of optional parameters was removed. Function Tester on AM Server nodes can be used to explore our toolkit library; double-clicking on a function there will display optional parameters in italics. As we add this annotation back in, we will use ="default" notation to highlight optional parameters along with their default value (e.g., see GetState).

== Basic functions ==

=== Assign(string var, string val, bool addToForceList) ===

Description: Assign a value to a variable (e.g, one defined in VAR_DEF); returns the value being assigned. Note that the first parameter is the name of the variable in quotes, not the variable itself!

The addToForceList argument should be set to “TRUE”, overriding the default of “FALSE” (which was made the default just to ensure backwards compatibility of legacy usage). Failure to do this could result in incorrect behavior for transactions that evalute results for multiple entities, such as L8 Tool Selection.

  Sample Usage:	=Assign(“SLOTS”,”4,5,6”,”TRUE”)  “4,5,6”

In general, variables are assigned by their VAR_DEF definition. There are unusual cases where it may be necessary to encode a side effect to override what is specified in VAR_DEF.

=== Echo(string expression) ===

Description: Evaluate the expression and return the result

  Sample Usage: =Echo(OPERATION)  value of OPERATION

Echo is useful for testing, especially if using the Evaluate Function String admin function. Note that there is shorthand for this: =OPERATION

=== Eval(params expressions) ===

Description: Evaluate all expressions and returns the result of the last expression

  Sample Usage: =Eval(HoldLot(“note”,”comment”),”TRUE”)  “TRUE”

Eval is useful for configuring side effects, such as setting lot attributes.

=== EvalVar(string var) ===

Description: Return the value of the variable with the name var. So, if you have a variable called VAR1 with the value “FOO”, then EvalVar(“VAR1”) [note the quotes around VAR1! Without them, it would try to evaluate the variable with the name “FOO”] would have the value “FOO”, which is same value as the expression VAR1. This function is useful when you are dynamically building the name of the variable you want to access.

'''Note:''' ''EvalVar can be replaced completely with VEval (although not vice-versa). That is, EvalVar(exp) is exactly equivalent to VEval(exp). Please use VEval instead so that we can deprecate EvalVar in the future.''

=== GenerateError(string errorText, bool suppressStack) ===

Description: Causes the transaction to immediately fail, returning the custom error message to the client. If this is part of the recipe selection model (either recipe lookup or Auto-RFC), then the error message will be displayed in the NTSC alert window. If the errorText isn’t specified, it will default to “User model invoked GenerateError”. 

  Sample Usage: =If(Equal(GetLotAttribute(“MyAtt”),”BadVal”),GenerateError(“Bad value in lot attribute”),”foo”)

GenerateError is useful for configuring pre-conditions to be enforced prior to lot processing.
suppressStack can be set to “TRUE” to prevent the full error message representing the evaluation stack from being displayed to clients. Note that Automation support prefers to see the stack, as it can help us quickly troubleshoot where the problem is in the model, so please use sparingly, such as when you have specs for users to respond to a specific error message:

Example error message when the default behavior is used:

  So Much Fun! (Evaluating =@VAR_TEST_FUNCTION.TEST_FUNCTION: =GenerateError("So Much Fun!")) (Evaluating =@RECIPE_RECIPENAME.RECIPENAME: =Concat(TEST_NAME, TEST_FUNCTION))

Example error message for the same example when the function string uses GenerateError("So Much Fun!", suppressStack: "TRUE") instead:

  So Much Fun! 


=== Try(string tryExpression, string defaultOnError) ===

Description: Attempts to evaluate tryExpression. If successful, the result of the evaluation is returned (and defaultOnError is not evaluated); otherwise, defaultOnError is evaluated and returned.

  Sample Usage: =Try(Quotient(17,3),"FROG")   “5”
  Sample Usage: =Try(Quotient(17,0),"FROG")   “FROG”

=== Random() ===
=== RandomInt(int minValue, string maxValue) ===

Description: Random number generator. Random returns a random floating point number between 0.0 and 1.0; RandomInt returns an integer between minValue (inclusive) and maxValue (exclusive).

  Sample Usage: =LessThan(Random(), 0.5)   50/50 “TRUE” vs. “FALSE”
  Sample Usage: =RandomInt(1, 7)   Random integer in the range [1,6]

=== SendEmail(string recipient, string body, string subject) ===

Description: Send an email to the specificied recipient(s) with the provided email subject and body. The function never generates an error and always returns “TRUE”, even if the recipient information is syntatically invalid.

  Sample Usage:	=SendEmail(“biff.henderson@intel.com”,STUFF,”Check this out!”)  “TRUE”

=== GetVersion() ===

Description: Returns the version of the active AMCT model which processed this transaction.

  Sample Usage:	=GerVersion()  “149”


=== GetSite() ===

Description: Returns the root of the AMUI tree. This could be useful for setup that varies across sites without having to modify it at each target site.

  Sample Usage:	=GetSite()  “D1D”
  Sample Usage:	=If(Equal(GetSite(),“D1D”), ... , ... )	

=== IsMatch(string value, string pattern) ===

Uses the same wildcard/glob syntax as AMCT key matching to allow user to check other values in the model. Returns “TRUE” if it is a match, “FALSE” otherwise. 

  Sample Usage: =IsMatch(“FROG”, “F[RL](AT|OG)”)  “TRUE”
  Sample Usage: =IsMatch(“FROG”, “B[RL](AT|OG)”)  “FALSE”

Note that IsMatch could replace other AMCT functions like StartsWith, EndsWith, Contains, and VContains. When the pattern is stored in a variable instead of just being a literal, the existing functions will be easier, so there are no plans to get rid of them. When the pattern is a literal, IsMatch is probably slightly more efficient than VContains for AMServer to execute, so please consider the switch (see below for example). 

   VContains(“FROG,TOAD,DUCK”,VAR) == IsMatch(VAR,“FROG|TOAD|DUCK”)

=== IsPTuple() ===

Description: Returns “FALSE” except for AMPX p-tuples, in which case it returns “TRUE”. If you think you need to use this function, you should review your plans with your Automation rep.

=== CheckSeq(string transition) ===

CheckSeq(n) is equivalent to And(GreaterThan(_SEQ,0),LessThanEqual(_SEQ,n)). The key difference is that it registers the transition point with AMPX to ensure that the recipe is reevaluated when the l-tuple crosses the threshold in a future MoveOut. Use of this is appropriate when you want to suppress logic for an l-tuple until it gets close to the target operation.

=== EvalRules(string table, string start) ===
	
Description: EvalRules is a convenience function for building a binary tree. table is the name of the table holding the rules, and defaults to “RULES”; start is the name of the start step, and defaults to “Start”. The next paragraph describes how EvalRules works in full, but it should be clear from the sample table and usage below.

AM will assign the start state to the variable STEP (which should be a key column in your rules table), then find the matching row in the table. It will then evaluate the expression in the CONDITION column; if it evaluates to “TRUE”, then the expression in the TRUE column will be evaluated and returned as the value of the EvalRules call, otherwise the expression in the FALSE column will be evaluated and returned. If the value of this expression starts with “Step:”, then the table will be reevaluated using the new value for STEP. As with most AM lookup tables, the user can add additional key columns to affect the correlation. CONDITION, TRUE, and FALSE are all value columns.

  Sample Usage: =EvalRules() -> “Finally!”

  1.	STEP = “Start”: CONDITION is not true, so return value of FALSE (an implicit new call to EvalRules)
  2.	STEP = “Step4”: CONDITION is true, so return value of TRUE (an implicit new call to EvalRules)
  3.	STEP = “Step3”: CONDITION is true, so return value of TRUE

  RULES

  STEP	CONDITION	TRUE	                       FALSE
  Start	false	        N/A	                      Step:Step4
  Step2	N/A	        N/A	                       N/A
  Step3	=LessThan(5,8)	Finally!                       N/A
  Step4	true	        =Concat("Step:","Step3")       N/A
  Step5	N/A	        N/A	                       N/A

== List functions ==

For these functions, a list is a string of comma-separated values (e.g., “abc,def,xyz” is a list of three values); only the Sort function requires that these values be numeric. The indexing of the list starts with 1. 

Several of the functions have an optional field delimiter that can be used to override the default value of “,”; this is the token that is used to separate the values in the original list. Functions that build a list for its return value may have an optional field join that can be used to override the default value of “,”; this is the token that is used to separate the values in the returned list.

=== Count(string list, string delimiter) ===
	
Description: Returns the number of items in the list.

  Sample Usage: =Count(“3,4,7,2,6”) -> 5

=== Index(string list, int position, string delimiter) ===

Description: Returns the item at index position in the list.

  Sample Usage: =Index(“3,4,7,2,6”,3) -> “7”

=== Chop(string list, string count, string delimiter, string join) ===

Description: Returns the first count items of the list.

  Sample Usage: =Chop(“3,4,7,2,6”,3) -> “3,4,7”

=== Union(params lists) ===

Description: Returns the union of all items in the lists (no sorting)

  Sample Usage: =Union(“1,4,9”,”2,4,6”,”5,6,7”) -> “1,4,9,2,6,5,7”

=== Sort(string list) ===

Description: Returns the result of sorting the list of numeric values

  Sample Usage: =Sort(“2,16,3.14,2”) -> “2,2,3.14,16”

=== StrSort(string list, string delimiter, string join, bool trim) ===

Description: Returns the result of sorting the list alphabetically. The function will by default trim whitespace from each item in the list.

  Sample Usage:	=StrSort(“ TOAD , FROG, DUCK”) -> “DUCK,FROG,TOAD”

=== Reverse(string list, string delimiter, string join) ===

Description: Returns the list of items in reverse order

  Sample Usage: =Reverse(“a,b,11,c”) -> “c,11,b,a”

=== Diff(string list1, string list2) ===

Description: Returns the list of items that are in list1 but not in list2 (i.e., list1 – list2)

  Sample Usage: =Diff(“a,b,c,d,e,f”,“b,e,n,d,”) -> “a,c,f”

=== VContains(string list, string item, string delimiter) ===

Description: Returns “TRUE” if the list contains the value, otherwise “FALSE”.

  Sample Usage: =VContains(“FOO,BAR”,“FOO”) -> “TRUE”
  Sample Usage: =VContains(“FOO,BAR”,“BA”) -> “FALSE”

=== Shuffle(string list, string delimiter, string join) ===

Description: Returns a copy of the list with its items shuffled.

  Sample Usage: =Shuffle(“CAT,DOG,PIG”)  -> “PIG,CAT,DOG” (one such result)

=== IndexOf(string list, string item, string delimiter) ===

Description: Returns the position of the first occurrence of the item in the list. If it doesn’t appear at all, it will return “”.

  Sample Usage: =IndexOf(“D,F,G,B,F”,”F”) -> 2
  Sample Usage: =IfDefined(IndexOf(“D,F,G,B,F”,”X”),GenerateError(“...”)) -> error

=== IsEqual(string set1, string set2, string delimiter) ===

Description: Returns “TRUE” if the two lists contain the exact same sets of items, regardless of the order of items in each list. Note that the two lists are treated as sets, meaning that duplicate values within a list are ignored (see the third example below).

  Sample Usage: =IsEqual(“1,2,3”,”2,3,1”) -> TRUE
  Sample Usage: =IsEqual(“1,2,3”,”1,2,3,4”) -> FALSE
  Sample Usage: =IsEqual(“1,2,3”,”1,2,3,1”) -> TRUE

=== VEval(string list) ===
Description: Converts each item in the list by evaluating it as if it were a function string with a ‘=’ in front of it, and returns the value of the last expression. As with the Eval function, this is most useful for generating side effects (HoldLot, SetLotAttribute, etc.). This function is more generally useful for lists containing a single item when you are dynamically building the expression (e.g., name of a variable).

  Sample Usage: =VEval(“VAR1,VAR2”) -> “TOAD” (where the variable VAR1 evaluates to “FROG”, and the variable VAR2 evaluates to “TOAD”.
  Sample Usage: =VEval(Concat(“VAR”,”1”)) -> “FROG” (the value of variable VAR1)
  Sample Usage: =VEval(“Add(3,5),Add(4,6),Add(2,2)”) -> “4” (the result of the final expression)

=== GenerateSequence(int index) ===

Description: Returns a list of numbers from 1 to index; index must be a positive integer.

  Sample Usage: =GenerateSequence(1) -> “1”
  Sample Usage: =GenerateSequence(5) -> “1,2,3,4,5”

== List Functions with Lambda Expressions ==

AM lambda expression functions allow the user to perform an operation on a numeric list by applying a function on each element of the list. The final argument is the function specification (which can use any standard or custom runtime function), where “_” should be used as the implicit variable representing the current element of the original list. 

'''Lambda Parameter''': These functions now have a final optional parameter “lambda” which should hold the name of a variable that will be assigned the value of the lambda.

  Sample Usage: If X2 is a variable defined as “=Concat(“*”,X1,”*”)”, then 
  =VConvertAll(“FROG,TOAD,GOAT”,X2,lambda:”X1”) -> “*FROG*,*TOAD*,*GOAT*”

The value of each item in the list is assigned to the variable X1 before evaluating X2. Note that you will have to add X1 to a variable definition table to avoid a model validation warning, although it’s defined LOOKUP_VALUE will not be used in this case.

'''Nested lambdas''' When nesting lambda expressions. "_" will hold the outermost lambda, and then subsequent nested lambdas will be assigned to "_1" thru "_9". This is an alternate solution to avoid having to assign the lambdas explicitly to other variables.

  =VConvertAll("A,B,C",VConvertAll("X,Y,Z",Concat(""_"",""_1""))) -> “AX,AY,AZ,BX,BY,BZ,CX,CY,CZ”

=== VConvertAll(string list, string func, string lambda, string delimiter, string join) ===

Description: Returns the a new list that contains the result of the provided function being applied to each element. “delimiter” and “join” can be used to specify, respectively, split and join delimiters other than the comma.

  Sample Usage: =VConvertAll(“2,3,4”,Product(“_”,2)) -> “4,6,8”
  Sample Usage: =VConvertAll(“2.3.4”,Product(“_”,2),delimiter:”.”,join:”-“) -> “4-6-8”

=== VAll(string list, string func, string lambda) ===
=== VAny(string list, string func, string lambda) ===

Description: Returns “TRUE” if each/any item in the list satisfies the predicate, otherwise “FALSE”

  Sample Usage: =VAll(“5,6,8,9”,GreaterThan(“_”,8)) -> “FALSE”
  Sample Usage: =VAny(“5,6,8,9”,GreaterThan(“_”,8)) -> “TRUE”

=== VCount(string list, string func, string lambda) ===
Description: Returns the number of items in the list that satisfy the predicate

  Sample Usage: =VCount(“5,8,9,6”,GreaterThan(“_”,6)) -> “2”

=== VWhere(string list, string func, string lambda) ===

Description: Returns the list of items in the original list that satisfy the predicate

  Sample Usage: =VWhere(“5,8,9,6”,GreaterThan(“_”,6)) -> “8,9”

=== VTakeWhile(string list, string func, string lambda) ===

Description: Returns items in the original list as long as the predicate is satisfied

  Sample Usage: =VTakeWhile(“5,8,9,6”,LessThan(“_”,9)) -> “5,8”
  Sample Usage: =VTakeWhile(“5,8,9,6”,GreaterThan(“_”,6)) -> “”

=== VSkipWhile(string list, string func, string lambda) ===

Description: Bypasses items in the original list as long as the predicate is satisfied, then returns the rest

  Sample Usage: =VSkipWhile(“5,8,9,6”,LessThan(“_”,9)) -> “9,6”
  Sample Usage: =VSkipWhile(“5,8,9,6”,GreaterThan(“_”,6)) -> “5,8,9,6”

=== VFirst(string list, string func, string lambda, string delimiter) ===
=== VLast(string list, string func, string lambda, string delimiter) ===

Description: Returns the first/last item in the original list that satisfies the predicate. If no items match the predicate, then “” is returned.

  Sample Usage: =VFirst(“5,8,9,6”,GreaterThan(“_”,6)) -> “8”
  Sample Usage: =VLast(“5,8,9,6”,GreaterThan(“_”,9)) -> “”

=== VSum(string list, string func, string lambda) ===
=== VMean(string list, string func, string lambda) ===
=== VMax(string list, string func, string lambda) ===
=== VMin(string list, string func, string lambda) ===

Description: Returns the result of the mathematical operation after applying the function to each item in the list. “” can be passed as the function, in which case the operation is applied to the original list (i.e., providing the function here is a shortcut so you don’t have to call VConvertAll first).

  Sample Usage: =VSum(“1,2,3”,Min(“_”,2)) -> “5”
  Sample Usage: =VMean(“3,9,5,4”,””) -> “5.25”
  Sample Usage: =VMax(“1,2,3”,Product(”_”,2)) -> “6”
  Sample Usage: =VMin(“1,2,3”,Product(”_”,2)) -> “2”

=== VMedian (string list, string func, string lambda) ===
=== VRange (string list, string func, string lambda) ===
=== VSigma(string list, string func, string lambda) ===

Description: Returns the median/range/sigma of the values in the list (after applying func to each item in the list).

  Sample Usage: =VMedian("3,9,5,4") -> “4.5”
  Sample Usage: =VRange("3,9,5,4") -> “6”
  Sample Usage: =VSigma("3,9,5,4") -> “2.63”

== Comparison functions ==

=== Equal(string term1, string term2) ===
=== NotEqual(string term1, string term2) ===

Description: Compares the results of the two expressions and returns “TRUE” or “FALSE”. The terms are treated as strings.

 Sample Usage: =Equal(OPERATION,”1234”)

=== GreaterThan(double term1, double term2) ===
=== GreaterThanEqual(double term1, double term2) ===
=== LessThan(double term1, double term2) ===
=== LessThanEqual(double term1, double term2) ===

Description: Compares the results of the two expressions – which must be numeric – and returns “TRUE” or “FALSE”. The terms must be numeric.

  Sample Usage: =LessThan(GetLotAttribute(“MagicNumber”),3.14)

== Logical functions ==

=== And(params expressions) ===

Description: Returns “TRUE” if all expressions evaluate to “TRUE”, otherwise “FALSE”

  Sample Usage: =And(Equal(ENTITY,”XYZ101”),Equal(GetLotCategory(“Custom”),”Foo”))

Note that And will stop evaluating once it reaches a value other than TRUE. Put more expensive expressions after other expressions. In the above example, if ENTITY has a value other than “XYZ101”, then the GetLotCategory function need not and will not be evaluated.

=== Or(params expressions) ===

Description: Returns “TRUE” if any expressions evaluates to “TRUE”, otherwise “FALSE”

  Sample Usage: =Or(Equal(ENTITY,”XYZ101”),Equal(GetLotCategory(“Custom”),”Foo”))

Note that Or will stop evaluating once it reaches a “TRUE” value. Put more expensive expressions after other expressions. In the above example, if ENTITY has the value “XYZ101”, then the GetLotCategory function need not and will not be evaluated.

=== Not(string booleanValue) ===

Description: Returns “TRUE” if the booleanValue expression evaluates to “FALSE”, otherwise “FALSE”.

  Sample Usage: =Not(LessThan(17,3.14)) -> “TRUE”

=== If(string condition, string term1, string term2) ===

Description: Evaluates and returns term1 if condition evaluates to “TRUE”; otherwise, evaluates and returns term2.  

  Sample Usage: =If(Equal(ENTITY,”XYZ101”),”0”,GetLotAttribute(“SomeCount”))

Note that only one of the two expressions will be evaluated. In the above example, if ENTITY has the value “XYZ101”, then the GetLotAttribute function need not and will not be evaluated.

=== IfDefined(params expressions) ===

Description: Returns the value of the first expression that evaluates to a non-blank value

  Sample Usage: =IfDefined(GetEntityAttribute(“InvalidAttr”),”FROG”,GetLotAttribute(“Owner”)) -> “FROG”

''Note that IfDefined will stop evaluating once it reaches a non-blank value. In the example above, the call to GetLotAttribute(“Owner”) would never be made.''

=== IsInteger(int stringToTest) ===
=== IsNumber(double val) ===

Description: Returns “TRUE” if the argument is an integer/number, otherwise “FALSE”. Note that commas will be treated as non-numeric.

  Sample Usage: =IsInteger(17) -> “TRUE”
  Sample Usage: =IsInteger(17.5) -> “FALSE”
  Sample Usage: =IsNumber(17.5) -> “TRUE”
  Sample Usage: =IsNumber(“17”) -> “TRUE”
  Sample Usage: =IsNumber(“FROG”) -> “FALSE”

== Math functions ==

=== Add(double term1, double term2) ===
=== Sub(double term1, double term2) ===
=== Product(double term1, double term2) ===

Description: Returns the sum/difference/product of term1 and term2. The terms must be numeric.

  Sample Usage: =Add(3.14,4) -> 7.14
  Sample Usage: =Sub(3.14,4) -> -0.86
  Sample Usage: =Product(3.14,4) -> 12.56

=== Max(params terms) ===
=== Min(params terms) ===

Description: Returns the maximum/minimum value of all terms. The terms must be numeric.

  Sample Usage: =Max(13,28,14,3.14) -> 28
  Sample Usage: =Min(13,28,14,3.14) -> 3.14

=== Quotient(string numerator, string denominator) ===
=== Mod(string number, string divisor) ===
=== Divide(double numerator, double denominator, string decimals) ===
	
Description: All functions employ floating point division. Quotient returns the whole part of the division; Mod returns the remainder. The Quotient and Mod functions were written to match Excel behavior, not .NET behavior; this difference is found when negative numbers are used (such as the second pair of examples below). Divide does basic floating point division. By default, it will be rounded off to 3 decimal places unless an argument is provided for “decimals”.

  Sample Usage: =Quotient(3.14,0.7) -> “4”
  Sample Usage: =Mod(3.14,0.7) -> “0.34”
  Sample Usage: =Quotient(-10,3) -> “-3”
  Sample Usage: =Mod(-10,3) -> “2”

=== Sqrt(double number, int places) ===
Description: Returns the square root of the number, rounded to the specified number of decimal places (default being 3).

  Sample Usage: =Sqrt(9.00) -> 3 
  Sample Usage: =Sqrt(8) -> 2.828 
  Sample Usage: =Sqrt(8, 2) -> 2.83 

=== Pow(double x, double y, int places) ===

Description: Returns a specified number (x) raised to the specified power (y), rounded to the specified number of decimal places (default being 3).

  Sample Usage: =Pow(2,3) -> 8
  Sample Usage: =Pow(2.5,3.5) -> 24.705
  Sample Usage: =Pow(2.5,3.5,4) -> 24.7053

== String functions ==

=== Len(string source) === 
  Description: Returns the length of the string
  Sample Usage: =Len(“abcde”) -> 5

=== Concat(params args) === 
  Description: Returns the concatenation of all argument
  Sample Usage: =Concat(“Hello, ”,ENTITY,“!”)

=== StrEqual(string str1, string str2) ===

  Case-insensitive version of Equal (and more efficient than using a combination of ToUpper/Equal). Returns “TRUE” if the two string parameters match ignoring case, “FALSE” otherwise.
  Sample Usage: =StrEqual(“FROG”,”frog”) -> “TRUE”
  Sample Usage: =Equal(“FROG”,”frog”) -> “FALSE”

=== Find(string findText, string withinText, int startPos, int count) ===	
=== Left(string source, int length) ===
=== Right(string source, int length) ===
=== Mid(string source, int startIndex, int length) ===

Description: These four functions work exactly like Excel’s string functions with the same name. Index starts at 1.
* Find returns the index of the first instance of findText within withinText, staring at position startPos (default of 1); if no instance is found, it returns -1. If count is specified, it will restrict the search to this number of characters
* Left returns the first length characters of source.
* Right returns the last length characters of source.
* Mid returns the middle length characters of source, starting at position startIndex. If length is not specified, it will copy until the end of the string.

  Sample Usage: =Find(“ABC”,”ABCDEFABC“,startPos:2) -> 7
  Sample Usage: =Left(”ABCDEFG“,3) -> “ABC”
  Sample Usage: =Right(”ABCDEFG“,3) -> “EFG”
  Sample Usage: =Mid(“ABCDEFG”,startIndex:4,length:3) -> “DEF”

=== Trim(string source) ===
  Description: Returns s with white space trimmed from the beginning and end of the string.
  Sample Usage: =Trim(“ xyz ”) -> “xyz”

=== ToUpper(string source) ===
=== ToLower(string source) ===

  Description: Returns s with the case modified.
  Sample Usage: =ToUpper (“abCD7z”) -> “ABCD7Z”
  Sample Usage: =ToLower (“abCD7z”) -> “abcd7z”

=== StartsWith(string source, string value) === 
=== EndsWith(string source, string value) === 
=== Contains(string source, string value) === 

  Description: Returns “TRUE” if source begins with / ends with / contains the substring value, otherwise “FALSE”.

  Sample Usage: =StartsWith(“abcde”,”abc”) -> “TRUE”
  Sample Usage: =EndsWith(“abcde”,”bce”) -> “FALSE”
  Sample Usage: =Contains(“abcde”,”bcd”) -> “TRUE”

=== Replace(string source, string oldValue, string newValue) ===

  Description: Returns a new string in which all occurrences of a specified string (oldValue) in the current string (source) are replaced with another specified string (newValue).

  Sample Usage: =Replace(“CABCABCA”, “CA”, “X”) -> “XBXBX” 
  Sample Usage: =Replace(“CABCABCA”, “CA”, “”) -> “BB” 
  Sample Usage: =Replace(“CABCABCA”, “CB”, “X”) -> “CABCABCA” 
  Sample Usage: =Replace(“CABCABCA”, “”, “X”) -> “CABCABCA” 
  Sample Usage: =Replace(“”, “CA”, “X”) -> “” 
  Sample Usage: =Replace(“CABCABCAB”, “CAB”, “”) -> “” 

=== PadLeft(string source, int width, char padChar) ===
=== PadRight(string source, int width, char padChar) ===

  Description: Returns a new string of a minimum specified length in which the beginning / end of the source string is padded with a character (default of “ “).
  
  Sample Usage: =PadLeft(“TOAD”,6) -> “  TOAD”
  Sample Usage: =PadLeft(“TOAD”,2) -> “TOAD”
  Sample Usage: =PadRight(“TOAD”,6,”$”) -> “TOAD$$”

== DateTime functions ==

DateTime functions can be used to enforce time-sensitive rules. For example, you could set up a rule that would only take effect for a specific week. 

The DateTime type represents a specific date and time. The format for specifying a DateTime literal is fairly flexible.  For example, “06/18/2009 17:00:00” is equivalent to “June 18 2009 5 PM”.  If you omit the time, then it is equivalent to 00:00 (midnight).

The TimeSpan type represents an increment of time, and has a particular format: d.h:m:s, with either side of the decimal being optional:
  “0:30:00” 		= 30 minutes
  “-3”		= -3 days
  “1.12:00:00”	= 1 day, 12 hours

=== Now() ===

Description: Returns the current timestamp

  Sample Usage: =Now() -> 06/18/2009 17:24:40

=== DTLessThan(DateTime dt1, DateTime dt2) ===
=== DTGreaterThan(DateTime dt1, DateTime dt2) ===

Description: Returns “TRUE” if dt1 is earlier/later than dt2, otherwise “FALSE”

  Sample Usage: =DTLessThan(Now(),"06/19/2009") -> “TRUE”
  Sample Usage: =DTGreaterThan(Now(),"06/19/2009") -> “FALSE”

=== TSLessThan(TimeSpan ts1, TimeSpan ts2) ===
=== TSGreaterThan(TimeSpan ts1, TimeSpan ts2) ===

Description: Returns “TRUE” if ts1 is less than ts2, otherwise “FALSE”

  Sample Usage: =TSLessThan(DTSubtract(Now(),MYDATE),”3”) -> Returns “TRUE” if the current date is within three days of MYDATE
  Sample Usage: =TSGreaterThan(DTSubtract(Now(),MYDATE),”6:00:00”) -> Returns “TRUE” if the current date is more than six hours after MYDATE

=== DTSubtract(DateTime dt1, DateTime dt2) ===

Description: Returns the difference of two dates (dt1-dt2) as a TimeSpan; generally, you should use this within another function which takes a TimeSpan as an argument (e.g., the examples under TSLessThan and TSGreaterThan)

=== DTAdd(DateTime dt, TimeSpan ts) ===

Description: Returns the result of adding the provided TimeSpan to the provided DateTime. The next several functions (starting with DTAddYears) have similar capability.

  Sample Usage: =DTAdd(Now(),”-3.12:00:00”) -> the date that is 3.5 days previous to the current time

=== DTAddYears(DateTime date, int numberOfYears) ===
=== DTAddMonths(DateTime date, int numberOfMonths) ===
=== DTAddDays(DateTime date, double numberOfDays)===
=== DTAddHours(DateTime date, double numberOfHours) ===
=== DTAddMinutes(DateTime date, double numberOfMinutes) ===
=== DTAddSeconds(DateTime date, int numberOfSeconds) ===

Description: Returns the result of adjusting the date by the provided amount

  Sample Usage: =DTAddMonths(“06/19/2009”,1) -> 07/19/2009 00:00:00
  Sample Usage: =DTAddDays(“06/19/2009”, 3.5) -> 06/22/2009 12:00:00
  Sample Usage: =DTAddHours(“06/19/2009”, -3.5) -> 06/18/2009 20:30:00

=== DTFormat(string date, string format) ===
Description: A useful function for extracting elements of the datetime. See Microsoft Reference for more examples.

  The below examples assume the current time is 12/14/2016 07:30:04 PM.
  Sample Usage:	=DTFormat(Now(),”MM”) -> “12”
  Sample Usage:	=DTFormat(Now(),”MMM”) -> “Dec”
  Sample Usage:	=DTFormat(Now(),”MMM”) -> “December”
  Sample Usage:	=DTFormat(Now(),”dd”) -> “14”
  Sample Usage:	=DTFormat(Now(),”ddd”) -> “Wed”
  Sample Usage:	=DTFormat(Now(),”dddd”) -> “Wednesday”
  Sample Usage:	=DTFormat(Now(),”yy”) -> “16”
  Sample Usage:	=DTFormat(Now(),”yyyy”) -> “2016”
  Sample Usage:	=DTFormat(Now(),”hh”) -> “07”
  Sample Usage:	=DTFormat(Now(),”HH”) -> “19”
  Sample Usage:	=DTFormat(Now(),”mm”) -> “30”
  Sample Usage:	=DTFormat(Now(),”H:mm”) -> “19:30”

== External functions ==

External functions are those that access data in automation systems outside of AM. Variables that hold the results of such functions are good candidates for caching (see Cached Context).

=== GetLotAttribute(string attributeName, string valIfBlank) ===
	
Description: Returns the value of the specified attribute for the lot currently being processed in the transaction. The value of valIfBlank (default = “[NULL]”) is returned for both of the following cases: (1) The UDA has a blank value in MES; (2) The UDA does not exist in MES. valIfBlank is lazy evaluated, so you can more elegantly generate an error via that argument if desired (rather than wrapping with an IfDefined, for example).

  Sample Usage: MYATT =GetLotAttribute(“Bogus”) -> “[NULL]”
  Sample Usage: MYATT2 =GetLotAttribute(“Bogus”,“”) -> “”

=== GetLotCategory(string categoryName) ===

	Description: Returns the value of the specified category for the lot currently being processed in the transaction.

        Sample Usage: =GetLotCategory(“ProcessType”) 

=== GetLotPlanAttribute(string attributeName, string defaultValue = null) ===

	Description: Returns the value of the specified attribute for the lot currently being processed in the transaction. Unlike GetLotAttribute, GetLotPlanAttribute does distinguish between the value being blank and it not existing; if it doesn't exist, then it will return the defaultValue if specified, otherwise it will throw an exception. (Note: defaultValue is new as of AM 3.1.6, available in VF in 2026)

        Sample Usage: =GetLotPlanAttribute("Purpose")  “AM Testing”
        Sample Usage: =GetLotPlanAttribute("BogusAttribute", "MyDefault")  “MyDefault”

 

=== IsHotLot() ===

	Description: Returns “TRUE” if MES considers the current transaction’s lot a hot lot, “FALSE” otherwise..

        Sample Usage: =IsHotLot()  “FALSE”

=== GetLotDetail(string detailName) ===

Description: A “secret” way to get information from the “raw” lot details return from MES. Unsure yet whether how consistent and stable the returned information is. For now, would prefer to just have this in reserve if a customer use case cannot be implemented any other way.

  Sample Usage: =GetLotDetail("MoveOutDateTime")  “20071219 22:49:53.00”

=== GetOperationDetail(string attributeName, string operationDetailCacheRefresh) ===

Description: Returns the value of the specified operation detail, using the current operation of the lot currently being processed in the transaction.  “operationDetailCacheRefresh” can bypass the default cache duration of 60 minutes if the value is expected to be more dynamic; units for this parameter are in minutes.

  Sample Usage: =GetOperationDetail(“SHORT_DESC”)

The following names fixed by AMServer are allowed: OPERPROCESS; AREA_MODULE; SHORT_DESC; LONG_DESC; FUNCTIONAL_AREA; VALID_EQP. This function can also access any detail that is in the MES reply; if you want to access such a detail, work with your AM rep to determine what name is being returned by MES.

=== GetCurrentLithoLayerReworkCounter() ===

Description: Returns the current layer rework count as calculated by MES (leveraging the ReworkLimitOnLithoLayers CRM table). For more information, contact MES.

  Sample Usage:	=GetCurrentLithoLayerReworkCounter()  “0”

=== GetLrdLastReticle() ===
=== GetLrdLastScanner() ===

	Description: Returns the value of the previous reticle/scanner on which the current lot was processed.

  Sample Usage: =GetLrdLastReticle()
  Sample Usage: =GetLrdLastScanner()

=== HoldLot(string note, string comment, string category) ===

Description: Attempts to put the lot on hold (with the provided note, comment, and optional category) for the lot currently being processed in the transaction. If category is not speficied, the default value “SPC” will be used; any other value used here must be defined in MES CRM – note that AM model validation currently does not validate the contetns. Returns “Y” if the HoldLot was successful, “N” otherwise.

  Sample Usage: =HoldLot(“AM model”,”Blocking lot from further processing”)

=== SetLotAttribute(string attributeName, string attributeValue) ===

Description: Attempts to set the attribute with the provided value for the lot currently being processed in the transaction. Always returns “TRUE”, for if AM is unable to set the lot attribute, then the entire transaction will immediately fail. 

  Sample Usage: =SetLotAttribute(“CustomAtt”,”Foo”)

=== AddLotComment(string comment, string commentType = "AMAdHoc") ===

Description: Adds a comment to the Flag Summary (as viewed in CLUI’s LotDetailView) for the current transaction’s lot and operation.

=== AddLotCommentByOperation(string comment, string commentType = "AMAdHoc", string operation = "", string route = "") ===

Description: Used to target an operation and/or route other than the current transactions. If route is not specified, it will use the lot's current route. To be verified: This can only target downstream.

=== IsChildLot() ===

Description: Returns “TRUE” if the lot currently being processed in the transaction is a child lot, otherwise “FALSE”.

  Sample Usage: =IsChildLot()

=== IsMomLot() ===

Description: Returns “TRUE” if the lot currently being processed in the transaction is a mom lot, otherwise “FALSE”.

  Sample Usage: =IsMomLot()

=== IsValidProduct(string product = null) ===

Description: Returns “TRUE” if the transaction lot's product (or specified product if provided) is valid in MES, otherwise “FALSE”.

  Sample Usage: =IsValidProduct()

=== GetProductAttribute(string attributeName, string product = null, string defaultIfNotExist = "") ===

Description: Returns the value of the specified attribute of the transaction lot's product (or specified product if provided); if the attribute does not exist, then defaultIfNotExist will be returned.

  Sample Usage: =GetProductAttribute("SomeAttribute", defaultIfNotExist:"MyDefault")

=== InsertAdhocMetroRoute(string adhocRoute, string before, string operation, string route) ===
=== InsertAdhocMetroRouteBatch(string adhocRoutes, string operations, string beforeFlags, string routes) ===

Description: Will insert the specified adhocRoute for the lot currently being processed in the transaction, either “before” or “after” (as specified in the before argument) the target operation/route. Returns “TRUE” if the insertion is successful, otherwise “FALSE”.

The default values for the operation and route arguments are OPERATION and ROUTE (i.e., the current operation/route for recipe selection transactions, and the target operation/route for lot selection transactions).

Note that currently this should not be used in lot selection models! The MoveOut will have a lock on the lot plan, preventing the route insertion. The standard usage we expect for now is as part of an AutoRFC model.

  Sample Usage: =InsertAdhocMetroRoute("MyAdhocRoute","After")

InsertAdhocMetroRouteBatch was designed to include multiple updates in one call. It has not been qualified yet; please engage with Automation if you wish to use this newer function.

=== GetUpstreamEntity(string operation, string tooltype) ===

Description: Returns the entity (or sub-entity) that the current lot most recently went through at the provided operation (or of the provided tooltype). If there is no match in the lot history, then the expression will evaluate to “”. Exactly one of operation and tooltype should be provided; otherwise, an error will be generated. 

  Sample Usage: =GetUpstreamEntity(“1000”) -> “XYZ401”
  Sample Usage: =GetUpstreamEntity(operation:”1000”) -> “XYZ401”
  Sample Usage: =GetUpstreamEntity(tooltype:”Scanner”) -> “XYZ401”
  Sample Usage: =GetUpstreamEntity(tooltype:“BogusTooltype”) -> “”

Beware: MES ARP enforces a 90-day retention limitation. 

=== GetUpstreamOperation(string tooltype) ===

Description: Returns the operation that the current lot most recently ran on a tool of the provided tooltype. If there is no such operation, then an error will be generated.

  Sample Usage: =GetUpstreamOperation(“Scanner”)  “123456”

=== GetEntityAttribute(string attributeName, string entity) ===
=== GetEntityCategory(string categoryName, string entity) ===
=== GetEntityAttributeSimple(string attributeName, string entity) ===

Description: Returns the value of the specified attribute/category for the specified entity. If the entity parameter is not specified, then the entity that is being targeted by the current transaction (i.e., standard variable ENTITY) will be used. If the category has multiple matches, then they will be concatenated. GetEntityAttributeSimple does a different optimization in the number of MES call; users should use GetEntityAttribute unless directed by their AM Automation rep to do otherwise.

  Sample Usage: =GetEntityAttribute("Availability","SNA708") -> "Down"
  Sample Usage: =GetEntityCategory("Contaminants","SNA708") -> "Au/NotAllowed,Cu/NotAllowed,Pb/NotAllowed"

=== GetEntityAvailability(string entity) ===
=== GetEntityState(string entity) ===

Description: Shortcuts for GetEntityAttribute(“Availability”) and GetEntityAttribute(“State”).

 
=== SetEntityAttribute(string attributeName, string attributeValue, string entity) ===
	
Description: Attempts to set the attribute with the provided value for the entity currently being processed in the transaction (or argument “entity”, if provided). 

Returns “TRUE” if the MES call succeeds, and “FALSE” if the call fails. 

  Sample Usage: =SetEntityAttribute(“CustomUDA”, “Foo”)

=== CheckRange(string name, double min, double max, string range, string type, string target, bool returnNum) ===

Note: The only “type” currently supported is MesEqpUda; the below description applies to other types, but will speak to MES Entity UDA for clarity.

Description: Used to do a MES Entity UDA check, using either the transaction’s ENTITY or the specified target. The caller must provide a non-empty min and/or max, or a non-empty range (syntax: “val1-val2”). AM Server will self-detect the type of the UDA value and do the appropriate check; if the UDA value is neither numeric nor a DateTime, the call will error out. 

Returns “TRUE” if within range, otherwise “FALSE”. If returnNum is set to “TRUE”, then a numeric value will be returned instead; -1 if below the range, 0 if within the range, and 1 if above the range. 

While CheckRange can be used to simplify And(LessThan, GreaterThan), including recognizing range syntax, the main driver for it is its intelligent integration with AMPX, which is a topic too big for this document. It suffices to say that CheckRange should be used to check numeric/DateTime MES UDA values.

=== CheckRanges(string name, string ranges, string type, string target) ===

Note: The only “type” currently supported is MesEqpUda; the below description applies to other types, but will speak to MES Entity UDA for clarity.

Description: Similar to CheckRange, used to do a MES Entity UDA check, using either the transaction’s ENTITY or the specified target. The difference here is that many ranges are checked at once; the same can be accomplished more awkwardly using CheckRange.

''In the below examples, assume TestAtt has the value 450.''
Returns the index of the range if within range, otherwise -1 if below the ranges and 0 if above the ranges:

  Sample Usage: CheckRanges("TestAtt","101-200,201-300,301-500,501-600") -> 3
  Sample Usage: CheckRanges("TestAtt","501-600,601-700,701-800") -> -1
  Sample Usage: CheckRanges("TestAtt","101-200,201-300,301-400") -> 0

Note that you’ll get strange behavior if there is a gap in the ranges:

  Sample Usage: CheckRanges("TestAtt"," 201-300,301-400,500-600") -> -1

You can also just configure the floor of each range, which gets around the issue of users configuring gaps. Range support was added for legacy user configuration; new usage should use floors:

  Sample Usage: CheckRanges("TestAtt","101,201,301,501-600") -> 3

You can also configure an associative array, in which case CheckRanges will return the associated item for the matching range if in range, and “” if out of range (whether above or below). This works with either ranges or floors:

  Sample Usage: CheckRanges("TestAtt","101-300:X,301-500:Y,501-600:Z") -> "Y"
  Sample Usage: CheckRanges("TestAtt","501:X,601:Y,701:Z") -> ""
  Sample Usage: CheckRanges("TestAtt","101:X,301:Y,501-600:Z") -> "Y"

 
=== CheckString(string name, string expected, string type, string target, bool useWild) ===

This function is like CheckRange (in its purpose and how to use it), except that it’s used to compare against a specific string value. This is useful when a UDA has a finite set of three or more values of which it can hold exactly one at any given time. The values are case-sensitive.

  Sample Usage: =CheckString(“L78GenericUDA8”, “FROG”)  will only return “TRUE” if the entity’s L78GenericUDA8 UDA currently has the value “FROG”.

The UDA value can have a list of values to match. Furthermore, if optional useWild is set to FALSE, one or more of these values can contain wildcards as per the AMCT key specification rules.

  Sample Usage: =CheckString(“L78GenericUDA8”, “FROG”)

  UDA Value	   Function Result
  FROG	                TRUE
  TOAD	                FALSE
  DUCK,FROG,TOAD	TRUE
  DUCK,FR*,TOAD	        TRUE (useWild = “TRUE”)

'''AMPX notes:''' 
* The function doesn’t handle blanks optimally with respect to AMPX, as AMPX will treat a blank value as “no filter”, resulting in it evaluating all certs that touch the UDA, just as if you didn’t use CheckString. 
* You can still only pass a single value to be matched to CheckString per transaction, and, unlike CheckRange, AMPX will not respond properly to UDA values if you call it with different values as part of a cert’s evaluation.

=== GetSubEntities(string entity, string filter) ===

Returns the list of subentities for the entity currently being processed in the transaction (or argument “entity”, if provided) in alphabetic order. The “filter” argument is used to restrict the list of children returned based on substring matching. If the filter is not provided in the function call, AM will use the model variable _MES_ENTITIES as the filter if it exists, otherwise it will return the list of all children. 

=GetSubEntities(“XYZ101”) -> “XYZ101_PC1,XYZ101_PC2,XYZ_LP1,XYZ_LP2”
=GetSubEntities(“XYZ101”, “PC”) -> “XYZ101_PC1,XYZ101_PC2”
=GetSubEntities(“XYZ101”, “FROG”) -> “”

=== HasLogicallyScrappedWafers() ===

Description: Returns “TRUE” if the lot currently being processed in the transaction contains wafers that are logically scrapped, otherwise “FALSE”.

  Sample Usage: =HasLogicallyScrappedWafers()

=== GetCarrier() ===
	
Description: Returns the carrierid for the current transactions’ lot. If no carrier is associated with the lot, than an error will be generated.

  Sample Usage: =GetCarrier()  “123456”

=== GetCarrierAttribute(string carrierId, string attributeName) ===
	
Description: Returns the value of the specified attribute for the specified carrier. This function was delivered so that AM Recipe Selection could be used on tools that process FOUPs rather than lots.
  Sample Usage: =GetCarrierAttribute(LOTID,"CarrierType") // for FOUP intros
  Sample Usage: =GetCarrierAttribute(GetCarrier(),"CarrierType") // for LOT intros

=== LogEqpEvent(string entity, string eventName, string comment) ===

Description: Logs the event against the specified MES entity (with the provided comment). Returns “TRUE” if the log event was successful and will generate an error if the backend call fails.

You may only use this function if the model also has the variable ENABLE_LOGEVENT defined as TRUE. At this time, this function is expected to only be used by select CA2 models.

The function currently restricts eventName to the following values: EqpInRepair, EqpOutOfControl, EqpUnschQual, EqpWaitingOperator, and EqpWaitingTechnician.

  Sample Usage: =LogEqpEvent(“XYZ501”, “EqpOutOfControl”, “My comment”)  “TRUE”

=== GetQueueTime(string tag, string refresh) ===
=== GetQueueCapacity(string minQueueTime, string maxQueueTime, string tag, string refresh) ===
=== GetQueueCapacityForCluster(string cluster, string operation, string minQueueTime, string maxQueueTime, string tag, string refresh) ===
	
Description: APF/L8 will do a virtual assignment of all orderable WIP to the available entities and return this information to AM.
* GetQueueTime returns the minimum queue wait time in minutes
* GetQueueCapacity returns the minimum queue time normalized between the upper and lower bounds provided

  Sample Usage: =GetQueueTime() -> 80
  Sample Usage: =GetQueueCapacity(0, 100) -> 0.8

Used to identify the L8 cluster are the operation (OPERATION by default, or if provided by the ForCluster functions) and the process (which will be the model’s AM process). The “cluster” provided in the ForCluster functions is used to route to the L8 rule; the value “DM-IQR” should suffice. The other methods will look for a model variable APF_CLUSTER to define this value (i.e., if you use GetQueueTime, set APF_CLUSTER to DM-IQR in VAR_DEF).

Optional argument “refresh” specifies a duration in minutes for which the APF result is cached across transactions. Automation will likely insist you set this value to reduce load on APF. Note that lot selection models will always retrieve fresh APF results. Optional argument “tag” is required if a given model has potential to reference different L8 clusters; the actual value for tag doesn’t matter, as long as the partitioning is consistent within your model.

=== GetDefectCount(string stepId, string path, string klarfType, string waferIds, string notFound) ===
=== GetDefectCountByFilename(string stepId, string filename, string notFound) ===

Description: Used to obtain the defect count from Klarity. Set notFound to “ERROR” to have an exception thrown.

=== LotPreScanned(string stepId, string deviceId, string waferIds) ===

Description: Returns TRUE if YAS considers all wafers scanned for the given step/device, and FALSE otherwise (in which case, details of which wafers were not scanned will be assigned to variable YAS_MSG). If waferIds is not specificed, the function will consider all waferids in the transaction’s lot’s current slotmap.

== Table Access functions ==

=== TryLookup(string tableName, string field, string defaultValue = "", bool doEval = "TRUE") ===

Description: Returns the result of an AMCT LOOKUP table lookup; if no matching row is found, then defaultValue is returned. =TryLookup("TABLE","FIELD","DEFAULT") is equivalent to the pattern =Try(@TABLE.FIELD,"DEFAULT"); TryLookup is a performance optimization and is the new BKM. Setting doEval to FALSE will return the raw cell contents (e.g., if the cell value is "=Add(3,5)", then the function string will be returned rather than the value "8").

=== Rows(string tableName, string filter = "", bool errorOnNoRows = false, bool firstMatchOnly = false) ===

=== Row(string tableName, string filter = "", bool errorOnNoRows = false) ===

=== RowValue(string tableName, string rows, string column) ===

Description: These three functions are intended to work in tandem, with the output of the Rows/Row function being used for the “rows” parameter in RowValue. 

Rows returns a list of rowids for any rows that match the current context. This could be an empty list of no rows match, or even a list of multiple rowids; if multiple rowids, then they will be returned in table sort order (as seen in AMUI). The filter is an optional list of key columns that will be ignored. If optional parameter errorOnNoRows is set to “TRUE”, then AM Server will throw the standard error message (the same one as if the table were referenced using @) if no rows match instead of returning “”. The firstMatchOnly parameter is deprecated; use Row instead. 

Row acts exactly the same as Row except that it only returns the first matched rowid.

RowValue returns the values in the column (which can be either a key column or value column) for the provided rowids.

Rows and RowValue are advanced functions that solve a slew of modeling problems but make the model trickier to manage. They are particularly dangerous to use in tables that use wildcards. It is advised that you engage with an Automation modeler prior to employing these in your model.

  ROWID	KEY1	KEY2	VAL
  100	FROG	DUCK	V1
  101	FROG	GOAT	V2
  102	TOAD	DUCK	V3
  103	T*	DUCK	V4
  104	T*	GOAT	V5

If the value of KEY1 is FROG and KEY2 is DUCK
  =Rows(“TABLE”) -> “100”
  =Rows(“TABLE”,”KEY2”) -> “100,101”
  =RowValue(“TABLE”,”100”,”VAL”) -> V1
  =RowValue(“TABLE”,”100,101”,”KEY2”) -> “DUCK,GOAT”

If the value of KEY1 is TOAD and KEY2 is DUCK
  =Rows(“TABLE”) -> “102,103” (!!!)

=== RecordState(string tablename) ===
=== GetState(string tablename, string field, string defaultValue = null) ===

=== DeleteState(string tablename) ===

Description: Used to read-and-write state information from custom tables (typically LOT tables). The values for the key/input columns in the custom table must all be valid for the current transaction (the values for these keys will be used to identify the row in the table). All functions will generate an error if the table is not found.

'''RecordState''': Adds or updates a row in the table. The values for all value/output columns will be calculated and recorded; an error will be generated if there is no matching variable for a value column. Always returns “TRUE” (if no error is generated).

  '''Warning:''' Using AMUI to add a new field to an in-use state table requires care. This is because changes to LOT tables happen immediately, not on check-in, so adding an invalid field will cause the RecordState to start failing in production until the new variable is active.  Below is the BKM for making such changes; by using this process, you should have no errors:

* If the variable corresponding to the new field is not yet valid for the model, first add the variable and then check in the model before proceeding to the next step.
* After the new variable is activated, check out the model again and add the new field to your LOT table.
  
'''GetState:''' Accesses a value/output column for a record. Alternate syntax to @TABLENAME.FIELD. Will generate an error if no row is matched (unless defaultValue is specified, in which case it will return that), or if the output column is not valid.

'''DeleteState:''' Deletes a row in the table. Always returns “TRUE” (if no error is generated), even if no matching row was found.

Below are some subsequent examples, using an example custom table @TABLE (key column OPERATION, output columns VAR1 & VAR2):

  OPERATION	VAR1	VAR2
		
  Sample Usage: =RecordState(“TABLE”) (assuming operation = 1000, VAR1 = VAL1, VAR2 = VAL2)

  OPERATION	VAR1	VAR2
  1000	        VAL1	VAL2

  Sample Usage: =RecordState(“TABLE”) (assuming operation = 1000, VAR1 = VAL1, VAR2 = VAL2.1)

  OPERATION	VAR1	VAR2
  1000	        VAL1	VAL2.1

  Sample Usage: =RecordState(“TABLE”) (assuming operation = 1001, VAR1 = VAL1, VAR2 = VAL2)

  OPERATION	VAR1	VAR2
  1000	        VAL1	VAL2.1
  1001	        VAL1	VAL2

  Sample Usage: =GetState(“TABLE”,VAR2) (assuming operation = 1000)  “VAL2.1”

  Sample Usage: =DeleteState(“TABLE”) (assuming operation = 1000)

  OPERATION	VAR1	VAR2
  1001	VAL1	VAL2

If deleteAll is set to TRUE, all matching rows will be deleted based on the transaction context. To use as intended, you will need to have the model resolve one of the variables to contain a wildcard; note that this is input matching, similar to LOT table filtering in AMUI, so a smaller set of wildcard options are available to you. Given the below table:

  OPERATION	KEY	VAR1	VAR2
  1000	        KEY1	VAL1	VAL2
  1000	        KEY1a	VAL1	VAL2
  1000	        KEY2	VAL1	VAL2
  1000	        KEY2a	VAL1	VAL2

  Sample Usage: =DeleteState(“TABLE”) (assuming operation = 1000 and KEY = KEY1*)

  OPERATION	KEY	VAR1	VAR2
  1000	        KEY2	VAL1	VAL2
  1000	        KEY2a	VAL1	VAL2

== Counter functions ==

These functions allow reading and writing to a state (LOT) table, with behavior driven by a LOOKUP table. While a user can create and use these tables and functions by themselves in AMUI, it is recommended that you engage with your AM Automation rep for initial setup. 


  Note: These functions may not work correctly in recipe selection models if used in a way that they are first called outside of the GetRecipe itself (e.g., CheckForReintro). This is because at this point AM considers the lot already introduced and thus by default suppresses incrementing the counter. Please consult your AM Automation rep if a workaround is needed.

In all cases, the functions act against a unique counter that is implicitly defined.
* The state of the counter is stored in the LOT table tag_COUNTERS.
* The configurable behavior of all counters is stored in the LOOKUP table tag_PARTITIONS.
* The unique counter for a given transaction is determined by AM Server from the context of the current transaction and the specification in the tag_PARTITIONS field.

''For folks familiar with AM Lot Selection, this generic counter behavior uses the same internal codebase as what is used for SKIPLOT_COUNTERS and SKIPLOT_PARTITIONS.''

=== CounterGetConfig(string tag, string parameter) ===

Description: Used to read values of output fields from tag_PARTITIONS. 

=== CounterGetState(string tag, string attribute) ===
=== CounterUpdateState(string tag, string value, string attribute) ===
=== CounterReset(string tag) ===
=== CounterIncrement(string tag, string attribute, int increaseBy) ===
=== CounterReachMax(string tag, bool doIncrement, bool doReset, int countIncreaseBy, int waferIncreaseBy) ===

Description: Used to read/write values of output fields from tag_COUNTERS. If attribute is not specified, then the COUNT field is accessed. If the recipe is a prelook (including L8), then no counter state changes are made.

* CounterReset is shorthand for resetting COUNT and the optional field WAFER_COUNT.  
* CounterIncrement is shorthand for incrementing the attribute (default COUNT) by increaseBy (default 1). 
* CounterReachMax returns TRUE if the counter reached its configured max. By default, it will increment the counter by 1 (and ignore the optional field WAFER_COUNT) and not automatically reset if it has reached the max. 

  ''There are many standard fields that can be used with this counter library, but most folks just stick with the basics. One day the standard fields will be documented here...''

= Appendix A: LOT table BKMs =
 
=== LOT vs LOOKUP vs MLOOKUP ===

Here are some of the main ways in which LOT and LOOKUP (including MLOOKUP) tables differ:

* LOOKUP tables require model checkout/checkin to be manually edited; LOT tables do not.
* LOOKUP tables are cloned upon manual edit and remain associated with the relevant model versions; a singular LOT table is shared between all model versions. As a result, manual edits to a LOOKUP table can be easily reverted via model rollback.
* Edits via Insert/Modify/DeleteRecord admin functions are the same for LOOKUP and LOT tables; they are applied immediately and cannot be reverted via model rollback.
* LOOKUP tables are generally used for user configuration and model logic; LOT tables are generally used for dynamic state and history.

The only differences between MLOOKUP and LOOKUP:
* Only Rows/RowValue can be used to access the contents of MLOOKUP tables from within AMCT; Model Validation will reject other techniques (although if you were to disable that MV error, they should still work fine).
* AMUI hides the ROW_ORDER column for MLOOKUP tables.

=== Different types of LOT tables ===

Two types of ways to access LOT tables are discussed below. While you can use any technique with any LOT table, the differences are manifested in AM Server performance. A modeler needs to determine how frequently a LOT table is updated and then use the appropriate technique. If unsure, consult Automation.

* '''Continually updated''': These tables are updated about as often as they are accessed, often with every single transaction of a particular type (e.g., GetRecipe). Tables that store “history” usually fall into this category. 
* '''Intermittently updated''': These tables are updated far less often than they are accessed. This can include tables that are manually edited (e.g., the user wants specific behavior for a particular lotid), and state tables that have infrequent, conditional state transitions. 

=== LOT table access techniques ===

* For tables that are continually updated, use the following techniques:
**Use GetState to read from the table
* For tables that are intermittently updated, use the following techniques:
** Use @, TryLookup, or Rows to read from the table;
** As with LOOKUP tables, if possible, mark the table as CachedLookup=Y;
** Note that such tables support the same table matching algorithm as LOOKUP tables (Autosort, wildcards, etc.), although this is uncommon.

= Appendix B: Configuring FUNCTION_DEF =

Below are the standard fields expected in FUNCTION_DEF for custom scripted functions (where the body of the function is in a script) or user-defined functions (where the body of the function is in AMCT itself). 

You can configure custom keys to control which versions of runtime functions are used for different calls (e.g., alpha testing a new version of a runtime function on a specific ENTITY). For such usage with custom scripted functions, you can put the new script in a separate folder, separate file, or a separate method; all scenarios are valid and up to the user’s preference. 

{| class="wikitable"
|-
! Field !! Type !! When Used !! Description
|-
| NAME	|| Input || Script UDF	|| The name of the runtime function used in configuration
|-
| FILE || Output || Script || The name of the file containing the implementation of the run-time function; you should get this value from your Automation rep.
|-
| METHOD || Output || Script || The name of the method in the script file which implements the run-time function; you should get this value from your Automation rep.
|-
| PARAMS || Output || Script UDF ||The signature of the method as expected in the configuration. This is used by model validation to ensure you are configuring the run-time function correctly. At this point, the only aspect of PARAMS that Model Validation is the number of parameters (e.g., not the type) as determined by the number of commas. You should get this value from your Automation rep.
|-
|FOLDER	|| Output || Script || The subfolder of the AM Scripts directory containing the script file; you should get this value from your Automation rep.
|-
|BODY	|| Output || UDF || The details of the function (see below section).
|-
|FORCE_RESOLVE	|| Output || Script UDF	|| If “TRUE”, then AM will force the reevaluation of all variables referenced within the function (except for those marked as CACHED=T). “TRUE” is the default behavior; unless you are sure you need this, please add FORCE_RESOLVE and set to “FALSE”.
|}

Table 9: FUNCTION_DEF fields

=== User-defined functions ===

The user-defined function (UDF) is a capability that enables the modeler to create their own functions to keep their models tidy. If you find that you have the same function string repeated throughout your model, perhaps with minor changes between the instances, you might consider employing this feature.

UDFs are defined in FUNCTION_DEF just like custom functions, but unlike custom functions, the definition of the function goes directly into FUNCTION_DEF instead of an external script. The only FUNCTION_DEF columns that should be filled out for UDFs are NAME (key), PARAMS (value), and BODY (value); NAME and PARAMS are also used for custom functions, whereas BODY is a new field used only for UDFs. 

Unlike custom functions, the names of the parameters in PARAMS are meaningful. Each time the function is invoked, the actual arguments are assigned to the parameters, which can then be used within BODY’s function string or elsewhere in the model if invoked by BODY. To demonstrate this last point, in the below example, the variable STRING (used within the BuildString body) is defined =Concat(S1,S2); in VAR_DEF (not shown here); note that the definition of STRING references the parameters defined as BuildString inputs.
 
Below are some examples of UDFs (as defined in FUNCTION_DEF):

{| class="wikitable"
|-
! NAME !! PARAMS !! BODY
|-
| NotifyBiff || MESSAGE	|| =SendEmail("biff.henderson@intel.com", MESSAGE, "AM notification")
|-
| DoubleXPlusY	|| X,Y	|| =Add(Product(X,2),Y)
|-
| IsEven || NUM	|| =And(GreaterThanEqual(NUM,0),Or(IsEven(Sub(NUM,2)),Equal(NUM,0)))
|-
| BuildString || S1,S2	|| =Concat("STRING: ",STRING)
|}

Table 10: Examples of User-Defined Functions

  =NotifyBiff("Hello!") -> Biff receives an email with subject "AM notification" and body "Hello!"

  =DoubleXPlusY(3,5) -> 11

  =IsEven(8) -> "TRUE"
  =IsEven(49) -> "FALSE"
  =IsEven(50) -> will error out; AM enforces a max recursion stack depth of 25 calls

  =BuildString("DUCK","FROG") -> "STRING: DUCKFROG"

= Appendix C: AM Server standard identifiers =

The purpose of this is to list all identifiers to help user avoid conflicts when they are choosing identifiers for their custom AM items. Only the names are listed; the descriptions will be scattered across the different configuration guides.

Regarding variables, there are many additional identifiers reserved by AM that start with a leading underscore; customer variables should always avoid starting with a leading underscore.

  Note: This appendix has not been updated since the previous version of the reference (v4.0, AM 1.5). Most standard variables we have added since then have a leading underscore.

  ALD_VERSION	    AM_ENABLED	           AM_MEASUREMENTSET	     AMINTROCOUNTER	    APF_CLUSTER	        AREA_MODULE
  CANDIDATETOOL	    CHECKLIST	           CURRENTOPERATION	     DC_ERROR	            DC_ERROR	        DC_ERROR_RESPONSE
  DISABLE_REINTRO   DO_RECIPE_LOOKUP	   ENABLE_GET_PREV_CONTEXT   EVENT	            EXCLUDE_PREV_WAFERS	F4ENTITY
  F4EVENT	    FORCE_EXCLUDE	   FORCE_INCLUDE	     FUNCTIONAL_AREA	    IN_MEASUREMENTSET	IS_TOOL_SPECIFIC
  LONG_DESC	    LONGWAFERIDS	   LOTID	             MEASUREMENTSETS	    MONITOR_SPCRISK	MONITORSETNAME
  NAME	            OCCUPIEDSLOTS	   OPERATION	             OPERPROCESS            PRODUCT	        QT_ARR
  QT_FINAL	    QT_MAX	           QT_MIN	             RECIPE	            RECIPE_SLOTS	RECIPE_WAFERIDS
  RECIPENEEDED	    ROUTE	           SHORT_DESC	             SHORTWAFERIDS	    SIFVARINCLUDED	SKIPLOT_ALLOW_CHILD
  SKIPLOT_LOG       SKIPLOT_FM_ON_REWORK   SKIPLOT_RUN_IQR	     SKIPLOT_RUN_IQR_AT_PQD SKIPLOT_T           SKIPLOT_ERROR_IF_NO_PARTITIONS_MATCHED

  SKIPLOT_TARGET_RATE	SKIPLOT_VA_DT_SOURCE	SPC_IDENTIFIER_ALL	SPC_MEASUREMENTSET   SPC_CHARTPOINTS_ALLVALID  SPC_IDENTIFIER	
  SPC_MONITORSET	SPC_SETUP	        SPC_USE_REF_FOR_MINMAX	TARGETOPERATION	     TOOL_ALLOWED	       TOTAL_REINTRO
  VA_ENTITY	        VA_OPER	                VA_ROOT			

Table 11: Standard AMServer variables


  AMTOSPC_MEASUREMENTSET_MAP	AMTOSPC_SESSION	      CLIENT_DEF	        DATACON_CONSDATA	DATACON_LOGIC	DATACON_SPCDATA
  FORCE_WAFERS	                FUNCTION_DEF	      HISTORY_RECIPE	        HISTORY_SKIPLOT	        HISTORY_WAFERS	LOGIC_REINTRO
  HOST_DEF	                MEASUREMENTSET_DEF    MEASUREMENTSET_MAP        RECIPE_DEF	        SKIPLOT_ATLARGE	SKIPLOT_COUNTERS
  SKIPLOT_FORCE	                SKIPLOT_LOGIC	      SKIPLOT_OPERS	        SKIPLOT_PARTITIONS	SKIPLOT_SPCFEEDFORWARDLOOKUP	SKIPLOT_SPCLOT
  SKIPLOT_SPCRULE	        SKIPLOT_VA_CONFIG     SKIPLOT_VA_EVENT_CONFIG	SKIPLOT_VA_EVENT_STATE	SKIPLOT_VA_LOT_STATE	        SKIPLOT_VA_STATE
  SPC_CHART_CATEGORY	        SPC_EVAL_VARS	      TOOL_FILTER	        VAR_DEF		

Table 12: Standard AMServer tables

[[Category:FSMRead]] [[Category:EndUserRead]]
[[Category:AM/Subpages]][[Category:AM]][[Category:AM_Training]][[Category:AIT Interests]]
